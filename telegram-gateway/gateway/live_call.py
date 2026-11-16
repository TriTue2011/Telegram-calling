"""Cuộc gọi Telegram THẬT 1-1: TDLib (báo hiệu) + NTgCalls (media).

Luồng gọi đi:  createCall (TDLib) -> updateCall Pending/ExchangingKeys -> Ready (có servers, khoá, emoji)
               -> NTgCalls: create_p2p_call, set_stream_sources, skip_exchange(khoá của TDLib), connect_p2p
               <-> báo hiệu hai chiều: NTgCalls on_signaling_data -> TDLib sendCallSignalingData,
                   TDLib updateNewCallSignalingData -> NTgCalls send_signaling_data.
Gọi đến:       updateCall (callStatePending, is_outgoing=False) -> chờ người dùng nghe (acceptCall) rồi cũng Ready.

NTgCalls trả awaitable gắn với vòng lặp asyncio: mọi lệnh NTgCalls chạy trong MỘT vòng lặp riêng (``CallRuntime.loop``).
Update của TDLib tới từ luồng nhận chung nên chỉ đẩy việc sang vòng lặp đó, không chặn.

Mỗi cuộc gọi giữ nhật ký sự kiện (``events``) để chẩn đoán từ xa qua ``GET /api/live/calls`` — định dạng PCM/hành vi
runtime của gọi riêng tư là điều phải đo bằng cuộc gọi thật.
"""

import asyncio
import base64
import logging
import os
import queue
import re
import shlex
import subprocess
import threading
import time
import uuid

from .engine import map_ready

logger = logging.getLogger("telegram_gateway")

SAMPLE_RATE = 48000
CHANNELS = 1
MAX_EVENTS = 200
MONITOR_EVERY = 3.0           # giây giữa hai lần đo media trong cuộc gọi
MAX_ENDED_CALLS = 20          # cuộc gọi đã kết thúc giữ lại để xem nhật ký; thừa thì bỏ cái cũ nhất


def p2p_enabled() -> bool:
    """Cho phép nối thẳng P2P với điện thoại? Mặc định TẮT (như ZALO_PC_P2P của Zalo Call): gateway chạy trong Docker bridge nên
    IP nó công bố là IP container (172.x) mà điện thoại không tới được; tắt thì cuộc gọi đi qua máy chủ chuyển tiếp của Telegram.
    Bật lại bằng TELEGRAM_P2P=1 (cần mạng host hoặc cổng UDP mở thẳng)."""
    return os.environ.get("TELEGRAM_P2P", "0").strip().lower() in ("1", "true", "yes", "on")


def mic_command(spec: str, fmt: str = "auto") -> str:
    """Lệnh shell xuất PCM16 48 kHz mono ra stdout (nguồn mic gửi lên cuộc gọi).

    spec: "test" (âm 440 Hz), "silence" (im lặng: nghe máy mà không đưa tiếng gì của nhà lên cuộc gọi), hoặc URL/tệp ffmpeg đọc được.
    fmt "s16le16k": URL là PCM16 16 kHz thô (mic R1)."""
    out = f"-f s16le -ac {CHANNELS} -ar {SAMPLE_RATE} pipe:1"
    base = "ffmpeg -hide_banner -loglevel error -nostdin"
    if spec == "silence":
        return f'{base} -re -f lavfi -i "anullsrc=r={SAMPLE_RATE}:cl=mono" {out}'
    if spec in ("", "test"):
        return f'{base} -re -f lavfi -i "sine=frequency=440:sample_rate={SAMPLE_RATE}" -af volume=0.25 {out}'
    src = shlex.quote(spec)
    if fmt == "s16le16k":
        return f"{base} -f s16le -ar 16000 -ac 1 -i {src} {out}"
    if spec.startswith(("rtsp://", "rtsps://")):
        return f"{base} -rtsp_transport tcp -i {src} -vn {out}"
    return f"{base} -i {src} -vn {out}"


# Loa nhận tiếng bên kia: CHỈ cửa bộ đàm của tích hợp r1_speaker / dahua_talk (`/api/<tích hợp>/<intercom|duplex>/<id>?k=<khoá>`).
# Không nhận URL tuỳ ý và không chạy lệnh tuỳ ý: gateway tự dựng lệnh ffmpeg cố định từ địa chỉ đã kiểm tra.
_SPEAKER_RE = re.compile(
    r"^https?://[A-Za-z0-9.\-]+(?::\d{1,5})?/api/(?:r1_speaker|dahua_talk)/(intercom|duplex)/[A-Za-z0-9_\-]+\?k=[A-Za-z0-9_\-%.~]+$")


def valid_speaker_url(url: str) -> bool:
    return bool(_SPEAKER_RE.match(url or ""))


def speaker_command(url: str) -> str:
    """Lệnh shell nhận PCM16 48 kHz mono ở stdin (tiếng bên kia) và POST lên cửa bộ đàm của loa: A-law 8 kHz cho ``intercom``
    (nửa song công, HA tự tắt mic loa lúc phát), PCM16 16 kHz thô cho ``duplex`` (song công, loa R1 tự khử vọng)."""
    m = _SPEAKER_RE.match(url or "")
    if not m:
        raise ValueError("speaker không phải địa chỉ cửa bộ đàm r1_speaker / dahua_talk")
    out = ("-ac 1 -ar 16000 -c:a pcm_s16le -f s16le" if m.group(1) == "duplex"
           else "-ac 1 -ar 8000 -c:a pcm_alaw -f alaw")
    return ("ffmpeg -hide_banner -loglevel error -fflags nobuffer -flags low_delay -probesize 32 -analyzeduration 0 "
            f"-f s16le -ar {SAMPLE_RATE} -ac {CHANNELS} -i pipe:0 {out} -flush_packets 1 -method POST -chunked_post 1 "
            f"-content_type application/octet-stream {shlex.quote(url)}")


def apply_defaults(c, d: dict) -> None:
    """Áp thiết bị MẶC ĐỊNH (mic, mic_format, camera, speaker, playback) lên một cuộc gọi chưa dựng media.

    Cuộc gọi ĐẾN được tạo với mic/camera «test» (âm 440 Hz, hình thử) nên phải áp mặc định trước khi nghe máy."""
    c.mic, c.mic_fmt, c.cam = d["mic"], d["mic_format"], d["camera"]
    if d.get("speaker") and valid_speaker_url(d["speaker"]):
        c.speaker_url, c.playback_kind = d["speaker"], "push"
    else:
        c.speaker_url = ""
        c.playback_kind = d.get("playback") if d.get("playback") in ("external", "file") else "file"


# Hình gửi đi: «low» cho máy yếu (đo thật: nặng nhất là giải mã + thu nhỏ luồng camera; ít điểm ảnh/giây hơn thì nhẹ hơn rõ).
VIDEO_PROFILES = {"normal": (640, 360, 15), "low": (480, 270, 10)}


def video_profile(env=None) -> tuple:
    """(rộng, cao, fps) theo biến môi trường VIDEO_PROFILE; giá trị lạ thì về «normal», không nổ lúc khởi động."""
    name = str((os.environ if env is None else env).get("VIDEO_PROFILE", "normal")).strip().lower()
    return VIDEO_PROFILES.get(name, VIDEO_PROFILES["normal"])


VIDEO_W, VIDEO_H, VIDEO_FPS = video_profile()


def camera_command(spec: str) -> str:
    """Lệnh shell xuất khung hình thô yuv420p (kích thước và fps theo VIDEO_PROFILE) ra stdout (nguồn hình gửi lên cuộc gọi video)."""
    out = f"-vf scale={VIDEO_W}:{VIDEO_H},fps={VIDEO_FPS} -f rawvideo -pix_fmt yuv420p pipe:1"
    base = "ffmpeg -hide_banner -loglevel error -nostdin"
    if spec == "black":              # đã nghe máy video nhưng chưa nối camera nào của nhà: gửi hình đen thay vì hình thử
        return f'{base} -re -f lavfi -i "color=c=black:size={VIDEO_W}x{VIDEO_H}:rate={VIDEO_FPS}" {out}'
    if spec in ("", "test"):
        return f'{base} -re -f lavfi -i "testsrc2=size={VIDEO_W}x{VIDEO_H}:rate={VIDEO_FPS}" {out}'
    src = shlex.quote(spec)
    if spec.startswith(("rtsp://", "rtsps://")):
        return f"{base} -rtsp_transport tcp -i {src} -an {out}"
    return f"{base} -re -i {src} -an {out}"



VIEW_W, VIEW_H, VIEW_FPS = 640, 360, 15      # mô tả cho NTgCalls: hình bên kia nhận về (khung thật mang kích thước riêng trong frame_data)
VIEW_OUT_FPS = 10


class VideoView:
    """Hình VIDEO của bên kia → JPEG mới nhất, để tầng REST phát MJPEG (``GET /api/live/view/<id>``) cho màn hình Dosen / trình duyệt.

    NTgCalls đưa khung I420 thô qua ``on_frames``; ffmpeg (tiến trình riêng của gateway) nén thành dãy JPEG, mỗi khi kích thước đổi thì dựng lại.
    Ghi vào ffmpeg qua hàng đợi nhỏ ở luồng riêng: ffmpeg chậm thì BỎ khung cũ, không chặn luồng NTgCalls."""

    def __init__(self, log=lambda *_: None) -> None:
        self.log = log
        self.jpeg = b""
        self.seq = 0
        self.size = None
        self.frames = 0
        self.dropped = 0
        self.bad = 0
        self._proc = None
        self._q = queue.Queue(maxsize=3)
        self._closed = False
        self._lock = threading.Lock()

    def push(self, data: bytes, width: int, height: int) -> None:
        if self._closed or width <= 0 or height <= 0:
            return
        if len(data) != width * height * 3 // 2:               # I420: Y + U + V; sai độ dài = khung lạ (có đệm?), bỏ và đếm
            self.bad += 1
            if self.bad == 1:
                self.log("khung video lạ: %dx%d nhưng %d byte (cần %d)" % (width, height, len(data), width * height * 3 // 2))
            return
        with self._lock:
            if self._proc is None or self.size != (width, height):
                self._restart(width, height)
        try:
            self._q.put_nowait(data)
            self.frames += 1
        except queue.Full:
            try:
                self._q.get_nowait()                            # bỏ khung cũ nhất, giữ khung mới
            except queue.Empty:
                pass
            self.dropped += 1
            try:
                self._q.put_nowait(data)
            except queue.Full:
                pass

    def _restart(self, width: int, height: int) -> None:
        self._stop_proc()
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "%dx%d" % (width, height),
               "-r", str(VIEW_FPS), "-i", "pipe:0", "-vf", "fps=%d" % VIEW_OUT_FPS, "-q:v", "6", "-f", "image2pipe", "-vcodec", "mjpeg", "-flush_packets", "1", "pipe:1"]
        try:
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as exc:
            self.log("không chạy được ffmpeg cho hình video: %s" % exc)
            self._proc = None
            return
        self.size = (width, height)
        self.log("hình video bên kia: %dx%d" % (width, height))
        threading.Thread(target=self._writer, args=(self._proc,), daemon=True).start()
        threading.Thread(target=self._reader, args=(self._proc,), daemon=True).start()

    def _writer(self, proc) -> None:
        while not self._closed and proc is self._proc:
            try:
                data = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
            except (OSError, ValueError):
                return

    def _reader(self, proc) -> None:
        """Tách dãy JPEG liền nhau ở stdout (mỗi ảnh bắt đầu FFD8, kết thúc FFD9)."""
        buf = b""
        while not self._closed and proc is self._proc:
            try:
                chunk = proc.stdout.read1(65536)         # read1: trả ngay phần đã có (read(n) sẽ chờ đủ n byte)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            buf += chunk
            while True:
                a = buf.find(b"\xff\xd8")
                b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                if a < 0 or b < 0:
                    buf = buf[a:] if a >= 0 else buf[-1:]
                    break
                self.jpeg = buf[a:b + 2]
                self.seq += 1
                buf = buf[b + 2:]

    def _stop_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._closed = True
        with self._lock:
            self._stop_proc()

    def info(self) -> dict:
        return {"frames": self.frames, "jpeg": self.seq, "size": "%dx%d" % self.size if self.size else "", "dropped": self.dropped, "bad": self.bad}


class LiveCall:
    def __init__(self, runtime, phone: str, client, user_id: int, outgoing: bool, video: bool = False,
                 mic: str = "test", mic_fmt: str = "auto", cam: str = "test"):
        self.runtime, self.phone, self.client = runtime, phone, client
        self.user_id, self.outgoing, self.video = int(user_id), outgoing, video
        self.mic, self.mic_fmt, self.cam = mic, mic_fmt, cam
        self.cam_off = False
        self.view = VideoView(self.log)    # hình video của bên kia (phát MJPEG cho màn Dosen)
        self.view_on = bool(video)         # đã xin NTgCalls đưa hình video bên kia về (cuộc gọi video, hoặc bên kia vừa bật camera)
        self.peer_video = False         # bên kia đang BẬT camera (xin chuyển sang video / đang gọi video)
        self.diag: dict = {}            # số đo media (mẫu mỗi vài giây): capture_s = thời gian nguồn GỬI ĐI đã chạy, ratio = capture_s / thời gian thật
        self._monitor_task = None
        self.id = uuid.uuid4().hex[:10]
        self.tdlib_call_id: int | None = None
        self.state = "dialing" if outgoing else "ringing"
        self.emojis: list = []
        self.events: list = []
        self.started_at = time.time()
        self.active_at = 0.0
        self.ended_at = 0.0
        self.frames_in = 0
        self.frame_bytes = 0
        self.playback_kind = "file"
        self.speaker_url = ""           # địa chỉ cửa bộ đàm của loa khi playback_kind == "push" (KHÔNG đưa ra info/log)
        self.playback_path = os.path.join(runtime.data_dir, "live", self.id + "_in.pcm")
        self.error = ""
        self.playback_bytes_final = None   # kích thước tệp tiếng bên kia lúc kết thúc (tệp đã xoá)
        self._ready_done = False
        self.log("tạo cuộc gọi %s video=%s" % ("đi" if outgoing else "đến", video))

    def log(self, text: str) -> None:
        self.events.append({"t": round(time.time() - self.started_at, 2), "e": text})
        del self.events[:-MAX_EVENTS]
        logger.info("live_call %s: %s", self.id, text)

    def info(self) -> dict:
        return {"id": self.id, "user_id": self.user_id, "outgoing": self.outgoing, "video": self.video,
                "state": self.state, "emojis": self.emojis, "error": self.error,
                "seconds": round((self.ended_at or time.time()) - (self.active_at or self.started_at), 1),
                "peer_video": self.peer_video, "view": self.view.info(), "diag": self.diag, "mic_muted": self.mic == "silence", "cam_off": self.cam_off, "frames_in": self.frames_in, "frame_bytes": self.frame_bytes, "peak": getattr(self, "peak", 0),
                "playback": {"kind": self.playback_kind, "bytes": self._playback_size()},
                "events": self.events[-40:]}

    def _drop_playback_file(self) -> None:
        """Kiểu `file` ghi tiếng bên kia ~96 KB/giây (≈345 MB mỗi giờ) và sau cuộc gọi không ai đọc lại: xoá khi kết thúc
        (nhớ kích thước để info còn báo được)."""
        self.playback_bytes_final = self._playback_size()
        for path in (self.playback_path, self.playback_path + ".wav"):
            try:
                os.remove(path)
            except OSError:
                pass

    def _playback_size(self) -> int:
        if self.playback_bytes_final is not None:
            return self.playback_bytes_final
        try:
            return os.path.getsize(self.playback_path)
        except OSError:
            return 0

    # -- TDLib -> chúng ta ---------------------------------------------------------------------------
    def on_call_state(self, call: dict) -> None:
        st = call.get("state") or {}
        kind = st.get("@type", "")
        if kind == "callStatePending":
            self._set("ringing" if st.get("is_received") else "dialing", "TDLib: pending created=%s received=%s"
                      % (st.get("is_created"), st.get("is_received")))
        elif kind == "callStateExchangingKeys":
            self._set("connecting", "TDLib: đang trao đổi khoá")
        elif kind == "callStateReady":
            if not self._ready_done:
                self._ready_done = True
                self.emojis = list(st.get("emojis") or [])
                try:
                    ours = list(self.runtime.protocol().get("library_versions") or [])
                except Exception:  # noqa: BLE001 - chỉ để ghi nhật ký
                    ours = []
                kinds = []
                for sv in st.get("servers") or []:                       # loại máy chủ (reflector/webrtc), cổng và cờ: quyết định kiểu đường truyền của cuộc gọi
                    t = sv.get("type") or {}
                    kinds.append("%s:%s%s%s%s" % ((t.get("@type") or "?").replace("callServerType", ""), sv.get("port"),
                                                  "+turn" if t.get("supports_turn") else "", "+stun" if t.get("supports_stun") else "", "+tcp" if t.get("is_tcp") else ""))
                try:
                    import json as _json
                    custom_keys = sorted((_json.loads(st.get("custom_parameters") or "{}") or {}).keys())
                except ValueError:
                    custom_keys = ["(không phải JSON)"]
                self._set("connecting", "TDLib: Ready (servers=%d %s, p2p=%s, emoji=%s, phiên bản bên kia=%s, phiên bản ta khai=%s, custom_parameters=%s)"
                          % (len(st.get("servers") or []), kinds, st.get("allow_p2p"), "".join(self.emojis),
                             list((st.get("protocol") or {}).get("library_versions") or []), ours, custom_keys))
                self.runtime.run(self._ready(st))
        elif kind == "callStateDiscarded":
            self._end("TDLib: bị kết thúc (%s)" % ((st.get("reason") or {}).get("@type", "?")))
        elif kind == "callStateError":
            self.error = str((st.get("error") or {}).get("message", "lỗi"))
            self._end("TDLib: lỗi %s" % self.error)
        elif kind == "callStateHangingUp":
            self.log("TDLib: đang cúp")

    def on_signaling(self, data: bytes) -> None:
        self.runtime.run(self._send_signaling(data))

    def _set(self, state: str, why: str) -> None:
        if state != self.state:
            self.state = state
        self.log(why)

    def _end(self, why: str) -> None:
        if self.state == "ended":
            return
        self.state, self.ended_at = "ended", time.time()
        self.log(why)
        self.runtime.run(self._stop())

    # -- NTgCalls (chạy trên vòng lặp riêng) -------------------------------------------------------
    async def _ready(self, ready: dict) -> None:
        ntg, nt = self.runtime.ntg, self.runtime.nt
        try:
            m = map_ready(ready)
            servers = []
            for s in m["servers"]:
                tag = s.get("peer_tag") or ""
                servers.append(nt.RTCServer(
                    int(s["id"]), s["ipv4"] or "", s["ipv6"] or "", int(s["port"]),
                    s["username"] or None, s["password"] or None, bool(s["turn"]), bool(s["stun"]), bool(s["tcp"]),
                    base64.b64decode(tag) if tag else None))
            self.log("NTgCalls: create_p2p_call")
            await ntg.create_p2p_call(self.user_id)
            self.log("NTgCalls: mic = %s" % ("âm thử" if self.mic in ("", "test") else "im lặng" if self.mic == "silence" else "nguồn ngoài"))
            if self.video:
                self.log("NTgCalls: camera = %s" % ("hình thử" if self.cam in ("", "test") else "nguồn ngoài"))
            await ntg.set_stream_sources(self.user_id, nt.StreamMode.CAPTURE, self._capture_description())
            await self._set_playback(self.playback_kind, self.speaker_url if self.playback_kind == "push" else "")
            if m["encryption_key"]:
                self.log("NTgCalls: skip_exchange")
                await ntg.skip_exchange(self.user_id, m["encryption_key"], self.outgoing)
            self.log("NTgCalls: connect_p2p (%d máy chủ)" % len(servers))
            await ntg.connect_p2p(self.user_id, servers, m["versions"], bool(m["p2p_allowed"]) and p2p_enabled(), m["custom_parameters"])
            self.log("NTgCalls: connect_p2p xong")
            if self._monitor_task is None:
                self._monitor_task = asyncio.ensure_future(self._monitor())
        except Exception as exc:  # noqa: BLE001 - lỗi runtime phải thấy được ở nhật ký, không nuốt
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self.log("NTgCalls LỖI " + self.error)
            logger.exception("live_call %s lỗi ở bước media", self.id)

    async def _monitor(self) -> None:
        """Đo media trong lúc gọi (chẩn đoán «tín hiệu mạng kém» ở đầu bên kia): mỗi MONITOR_EVERY giây hỏi NTgCalls nguồn GỬI ĐI đã chạy được bao lâu.
        Nguồn mic/camera chạy đúng nhịp thì capture_s tăng bằng thời gian thật (ratio ≈ 1); nguồn bị đói (ffmpeg không có dữ liệu, URL treo) thì ratio tụt xuống,
        và nhật ký ghi «nguồn gửi đi chậm». Không ném lỗi ra ngoài; dừng khi cuộc gọi kết thúc."""
        nt, ntg = self.runtime.nt, self.runtime.ntg
        t0 = last_wall = time.monotonic()
        last_cap = None
        try:
            while self.state in ("connecting", "active"):
                await asyncio.sleep(MONITOR_EVERY)
                now = time.monotonic()
                try:
                    cap = float(await ntg.time(self.user_id, nt.StreamMode.CAPTURE))
                except Exception as exc:  # noqa: BLE001 - chưa dựng xong / đã dừng: thử lại lượt sau
                    self.diag = {"loi": type(exc).__name__}
                    continue
                if last_cap is not None and now - last_wall > MONITOR_EVERY / 2:
                    ratio = round((cap - last_cap) / (now - last_wall), 2)
                    self.diag = {"capture_s": round(cap, 1), "wall_s": round(now - t0, 1), "ratio": ratio}
                    if ratio < 0.5:
                        self.log("nguồn gửi đi chậm: chạy %.0f%% so với thời gian thật (mic/camera đói dữ liệu?)" % (ratio * 100))
                last_cap, last_wall = cap, now
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.log("đo media dừng: %s" % type(exc).__name__)

    def _capture_description(self):
        """Mô tả nguồn GỬI ĐI (mic + camera nếu là cuộc gọi video) theo trạng thái hiện tại của cuộc gọi."""
        nt = self.runtime.nt
        cam = None
        if self.video and not self.cam_off:
            cam = nt.VideoDescription(nt.MediaSource.SHELL, VIDEO_W, VIDEO_H, VIDEO_FPS, camera_command(self.cam), True)
        return nt.MediaDescription(
            nt.AudioDescription(nt.MediaSource.SHELL, SAMPLE_RATE, CHANNELS, mic_command(self.mic, self.mic_fmt), True), None, cam, None)

    async def set_capture(self, mic: str | None = None, mic_fmt: str | None = None, cam: str | None = None, cam_off: bool | None = None,
                          video: bool | None = None) -> None:
        """Đổi mic/camera NGAY trong cuộc gọi (nói trực tiếp, tắt mic = ``silence``, tắt hình). Chỉ nguồn đã đổi mới thay; không ném lỗi ra ngoài vòng lặp NTgCalls."""
        if mic is not None:
            self.mic = mic or "test"
        if mic_fmt is not None:
            self.mic_fmt = mic_fmt
        if cam is not None:
            self.cam = cam or "test"
        if cam_off is not None:
            self.cam_off = bool(cam_off)
        if video:                                  # bật video giữa cuộc gọi thoại (đồng ý lời xin chuyển sang video): bắt đầu gửi hình
            self.video, self.cam_off = True, False
        if self.state in ("connecting", "active"):
            await self.runtime.ntg.set_stream_sources(self.user_id, self.runtime.nt.StreamMode.CAPTURE, self._capture_description())
        self.log("đổi nguồn gửi đi: mic=%s camera=%s" % ("im lặng" if self.mic == "silence" else "nguồn ngoài" if self.mic != "test" else "âm thử",
                                                         "tắt" if self.cam_off else "bật" if self.video else "không có"))

    async def refresh_capture(self, delays: tuple = (1.5, 5.0)) -> None:
        """Cuộc gọi VIDEO ĐI: bật lại nguồn gửi đi SAU KHI đã nối xong. Nguồn được đặt sớm (lúc TDLib báo Ready, trước khi nối) và điện thoại hay chỉ thấy ô hình đen
        vì khung khoá đầu tới trước khi bên kia sẵn sàng nhận; zalo_call làm ngược lại: chỉ bật media ở sự kiện «đã nghe máy». Đặt lại nguồn ép gửi khung khoá mới. Lỗi chỉ ghi nhật ký."""
        for wait in delays:
            await asyncio.sleep(wait)
            if self.state != "active":
                return
            try:
                await self.runtime.ntg.set_stream_sources(self.user_id, self.runtime.nt.StreamMode.CAPTURE, self._capture_description())
                self.log("bật lại nguồn gửi đi sau khi nối xong (+%.1f s)" % wait)
            except Exception as exc:  # noqa: BLE001
                self.log("bật lại nguồn gửi đi lỗi: %s" % exc)

    async def say(self, pcm: bytes) -> float:
        """Đọc một đoạn tiếng (PCM s16le 48 kHz mono, ví dụ TTS) VÀO cuộc gọi: mic chuyển sang đoạn đó, đọc xong trả mic về như cũ
        (trừ khi trong lúc đọc có ai đổi mic khác). Trả số giây đoạn tiếng; hàm này chạy tới khi đọc xong."""
        import wave
        seconds = len(pcm) / (SAMPLE_RATE * 2 * CHANNELS)
        os.makedirs(os.path.join(self.runtime.data_dir, "live"), exist_ok=True)
        path = os.path.join(self.runtime.data_dir, "live", self.id + "_say.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(2)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        prev = (self.mic, self.mic_fmt)
        try:
            await self.set_capture(mic=path, mic_fmt="auto")
            await asyncio.sleep(seconds + 0.4)
            if self.mic == path and self.state != "ended":
                await self.set_capture(mic=prev[0], mic_fmt=prev[1])
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
        return seconds

    async def _set_playback(self, kind: str, cmd: str = "") -> None:
        """Cách nhận tiếng bên kia: external (khung về on_frames), file (NTgCalls ghi PCM thô vào tệp),
        shell (lệnh nhận PCM ở stdin), push (đẩy thẳng vào cửa bộ đàm của loa; ``cmd`` là địa chỉ đó). Đổi được ngay trong lúc gọi."""
        nt = self.runtime.nt
        os.makedirs(os.path.dirname(self.playback_path), exist_ok=True)
        if kind == "external":
            src, inp = nt.MediaSource.EXTERNAL, ""
        elif kind == "push":
            url = cmd or self.speaker_url
            self.speaker_url = url
            src, inp = nt.MediaSource.SHELL, speaker_command(url)        # ValueError nếu không phải cửa bộ đàm hợp lệ
        elif kind == "shell":
            src = nt.MediaSource.SHELL
            inp = cmd or ("ffmpeg -hide_banner -loglevel error -f s16le -ar %d -ac %d -i pipe:0 -f wav %s.wav"
                          % (SAMPLE_RATE, CHANNELS, shlex.quote(self.playback_path)))
        else:
            kind, src, inp = "file", nt.MediaSource.FILE, self.playback_path
        self.playback_kind = kind
        self.log("NTgCalls: nhận tiếng bằng %s" % kind)
        cam = nt.VideoDescription(nt.MediaSource.EXTERNAL, VIEW_W, VIEW_H, VIEW_FPS, "", False) if self.view_on else None
        if cam is not None:
            self.log("NTgCalls: nhận cả hình video bên kia (khung về on_frames)")
        await self.runtime.ntg.set_stream_sources(self.user_id, nt.StreamMode.PLAYBACK, nt.MediaDescription(
            None, nt.AudioDescription(src, SAMPLE_RATE, CHANNELS, inp, False), cam, None))

    async def enable_view(self) -> None:
        """Bên kia vừa bật camera giữa cuộc thoại: xin NTgCalls đưa cả hình về (dựng lại mô tả PLAYBACK, giữ nguyên đường nhận tiếng)."""
        if self.view_on or self.state not in ("connecting", "active"):
            return
        self.view_on = True
        try:
            await self._set_playback(self.playback_kind, self.speaker_url if self.playback_kind == "push" else "")
        except Exception as exc:  # noqa: BLE001 - không được làm hỏng cuộc gọi đang chạy
            self.view_on = False
            self.log("không bật được nhận hình video: %s %s" % (type(exc).__name__, str(exc)[:100]))

    async def _send_signaling(self, data: bytes) -> None:
        try:
            await self.runtime.ntg.send_signaling_data(self.user_id, bytes(data))
        except Exception as exc:  # noqa: BLE001
            self.log("NTgCalls send_signaling_data lỗi: %s" % exc)

    async def _stop(self) -> None:
        self.view.close()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
        try:
            await self.runtime.ntg.stop(self.user_id)
        except Exception as exc:  # noqa: BLE001 - chưa tạo cuộc gọi NTgCalls thì stop báo lỗi: bình thường
            self.log("NTgCalls stop: %s" % exc)
        finally:
            self._drop_playback_file()

    # -- do người dùng ---------------------------------------------------------------------------------
    def hangup(self) -> None:
        if self.tdlib_call_id is not None and self.state != "ended":
            try:
                talked = int(time.time() - self.active_at) if self.active_at else 0
                self.client.discard_call(self.tdlib_call_id, duration=talked, is_video=bool(self.video))
            except Exception as exc:  # noqa: BLE001
                self.log("discardCall lỗi: %s" % exc)
        self._end("người dùng cúp")

    def accept(self) -> None:
        self.client.accept_call(self.tdlib_call_id, self.runtime.protocol())
        self.log("đã nghe máy")


def sweep_stale_audio(data_dir: str) -> None:
    """Lúc khởi động chưa có cuộc gọi nào: tệp tiếng bên kia còn lại là của lần chạy trước (sập, hoặc bản cũ không xoá)."""
    live = os.path.join(data_dir, "live")
    try:
        names = os.listdir(live)
    except OSError:
        return
    for name in names:
        if "_in.pcm" in name:
            try:
                os.remove(os.path.join(live, name))
            except OSError:
                pass


class CallRuntime:
    """Một vòng lặp asyncio + một NTgCalls cho cả tiến trình; định tuyến sự kiện theo user_id."""

    def __init__(self, on_event=None, data_dir: str = "/data"):
        import ntgcalls
        self.data_dir = data_dir
        self.nt = ntgcalls
        self.ntg = ntgcalls.NTgCalls()
        self.loop = asyncio.new_event_loop()
        self._t = threading.Thread(target=self.loop.run_forever, name="ntg-loop", daemon=True)
        self._t.start()
        self.calls: dict[str, LiveCall] = {}
        self.on_event = on_event or (lambda kind, call: None)
        self.ntg.on_signaling_data(self._sig)
        self.ntg.on_connection_change(self._conn)
        self.ntg.on_frames(self._frames)
        self.ntg.on_stream_end(self._stream_end)
        self.ntg.on_remote_source_change(self._remote_source)
        self.ntg.on_upgrade(self._upgrade)
        self._first_frame_logged = False

    def _prune(self) -> None:
        """Giữ tối đa MAX_ENDED_CALLS cuộc gọi đã kết thúc (cái cũ nhất đi trước); cuộc đang chạy không bao giờ bị bỏ."""
        ended = sorted((c for c in list(self.calls.values()) if c.state == "ended"), key=lambda c: c.ended_at)
        for c in ended[:max(0, len(ended) - MAX_ENDED_CALLS)]:
            self.calls.pop(c.id, None)

    def run(self, coro) -> None:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: f.exception() and logger.error("tác vụ NTgCalls lỗi: %r", f.exception()))

    def protocol(self) -> dict:
        p = self.nt.NTgCalls.get_protocol()
        return {"@type": "callProtocol", "udp_p2p": bool(p.udp_p2p) and p2p_enabled(), "udp_reflector": bool(p.udp_reflector),
                "min_layer": int(p.min_layer), "max_layer": int(p.max_layer),
                "library_versions": list(p.library_versions)}

    def by_user(self, user_id: int) -> "LiveCall | None":
        for c in list(self.calls.values()):
            if c.user_id == int(user_id) and c.state != "ended":
                return c
        return None

    # NTgCalls -> chúng ta (chạy ở luồng của NTgCalls)
    def _sig(self, user_id, data):
        c = self.by_user(user_id)
        if c is not None and c.tdlib_call_id is not None:
            try:
                c.client.send_signaling_data(c.tdlib_call_id, bytes(data))
            except Exception as exc:  # noqa: BLE001
                c.log("sendCallSignalingData lỗi: %s" % exc)

    def _conn(self, user_id, info):
        c = self.by_user(user_id)
        if c is None:
            return
        state = getattr(info, "state", None)
        name = getattr(state, "name", str(state))
        c.log("NTgCalls kết nối: %s" % name)
        if name == "CONNECTED" and not c.active_at:
            c.active_at, c.state = time.time(), "active"
            self.on_event("active", c)
            if c.outgoing and c.video:
                self.run(c.refresh_capture())
        elif name in ("FAILED", "TIMEOUT", "CLOSED") and c.state != "ended":
            c._end("NTgCalls: %s" % name)
            self.on_event("ended", c)

    def _frames(self, user_id, mode, device, frames):
        c = self.by_user(user_id)
        if c is None:
            return
        if getattr(device, "name", str(device)) == "CAMERA":                 # khung HÌNH của bên kia (I420), không phải tiếng
            for fr in frames:
                fd = getattr(fr, "frame_data", None)
                c.view.push(getattr(fr, "data", b"") or b"", int(getattr(fd, "width", 0) or 0), int(getattr(fd, "height", 0) or 0))
            return
        for fr in frames:
            c.frames_in += 1
            data = getattr(fr, "data", b"") or b""
            c.frame_bytes += len(data)
            if len(data) >= 2:
                n = min(len(data) // 2, 960)
                try:
                    import struct
                    vals = struct.unpack("<%dh" % n, data[:n * 2])
                    c.peak = max(getattr(c, "peak", 0), max(abs(x) for x in vals))
                except Exception:  # noqa: BLE001
                    pass
            if not self._first_frame_logged:
                self._first_frame_logged = True
                c.log("khung đầu: mode=%s device=%s thuộc tính=%s byte=%d"
                      % (getattr(mode, "name", mode), getattr(device, "name", device),
                         [a for a in dir(fr) if not a.startswith("_")][:8], len(data)))

    def _set_peer_video(self, c, on: bool, why: str) -> None:
        if c.peer_video != on:
            c.peer_video = on
            c.log("bên kia %s camera (%s)" % ("BẬT" if on else "tắt", why))
            self.on_event("peer_video", c)
            if on and not c.view_on:
                self.run(c.enable_view())

    def _remote_source(self, user_id, src):
        """Nguồn từ xa đổi trạng thái (camera/mic của bên kia bật hoặc tắt)."""
        c = self.by_user(user_id)
        if c is None:
            return
        dev = getattr(getattr(src, "device", None), "name", str(getattr(src, "device", "")))
        st = getattr(getattr(src, "state", None), "name", str(getattr(src, "state", "")))
        c.log("nguồn từ xa: %s %s" % (dev, st))
        if dev == "CAMERA":
            self._set_peer_video(c, st == "ACTIVE", "nguồn từ xa")

    def _upgrade(self, user_id, media):
        """Bên kia đổi trạng thái media (tắt tiếng, bật/tắt video)."""
        c = self.by_user(user_id)
        if c is None:
            return
        stopped = bool(getattr(media, "video_stopped", True))
        paused = bool(getattr(media, "video_paused", False))
        c.log("media bên kia: muted=%s video_stopped=%s video_paused=%s" % (getattr(media, "muted", None), stopped, paused))
        self._set_peer_video(c, not stopped and not paused, "trạng thái media")

    def _stream_end(self, user_id, stype, device):
        c = self.by_user(user_id)
        if c is not None:
            c.log("luồng kết thúc: %s/%s" % (getattr(stype, "name", stype), getattr(device, "name", device)))

    # -- API cho tầng REST -----------------------------------------------------------------------------
    def call_out(self, phone: str, client, user_id: int, video: bool = False, mic: str = "test",
                 mic_fmt: str = "auto", cam: str = "test", playback: str = "file", speaker_url: str = "") -> LiveCall:
        if self.by_user(user_id) is not None:
            raise RuntimeError("đang có cuộc gọi với người này")
        c = LiveCall(self, phone, client, user_id, True, video, mic, mic_fmt, cam)
        c.playback_kind = playback if playback in ("external", "file", "shell") else "file"
        if speaker_url:
            if not valid_speaker_url(speaker_url):
                raise ValueError("speaker không phải địa chỉ cửa bộ đàm r1_speaker / dahua_talk")
            c.speaker_url, c.playback_kind = speaker_url, "push"
        self.calls[c.id] = c
        self._prune()
        new_id = client.create_call(user_id, self.protocol(), video)
        if c.tdlib_call_id is None:
            c.tdlib_call_id = new_id
        c.log("TDLib: createCall -> id %s" % new_id)
        return c

    def on_tdlib_update(self, phone: str, client, obj: dict) -> None:
        """Gọi từ client.on_update cho MỌI update của tài khoản (nhanh, không chặn)."""
        t = obj.get("@type")
        if t == "updateCall":
            call = obj.get("call") or {}
            cid = call.get("id")
            c = next((x for x in list(self.calls.values()) if x.tdlib_call_id == cid), None)
            if c is None and call.get("is_outgoing"):
                # updateCall đầu tiên có thể tới TRƯỚC phản hồi createCall: nhận cuộc gọi đi chưa có mã, cùng người.
                c = next((x for x in list(self.calls.values()) if x.outgoing and x.tdlib_call_id is None
                          and x.user_id == int(call.get("user_id") or 0) and x.state != "ended"), None)
                if c is not None:
                    c.tdlib_call_id = cid
            if c is None and not call.get("is_outgoing"):
                c = LiveCall(self, phone, client, call.get("user_id"), False, bool(call.get("is_video")))
                c.tdlib_call_id = cid
                self.calls[c.id] = c
                self._prune()
                self.on_event("incoming", c)
            if c is not None:
                c.on_call_state(call)
                if c.state == "ended":
                    self.on_event("ended", c)
        elif t == "updateNewCallSignalingData":
            c = next((x for x in list(self.calls.values()) if x.tdlib_call_id == obj.get("call_id")), None)
            if c is not None:
                raw = obj.get("data") or ""
                c.on_signaling(base64.b64decode(raw) if isinstance(raw, str) else bytes(raw))
