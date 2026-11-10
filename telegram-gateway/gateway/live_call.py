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
import shlex
import threading
import time
import uuid

from .engine import map_ready

logger = logging.getLogger("telegram_gateway")

SAMPLE_RATE = 48000
CHANNELS = 1
MAX_EVENTS = 200


def mic_command(spec: str, fmt: str = "auto") -> str:
    """Lệnh shell xuất PCM16 48 kHz mono ra stdout (nguồn mic gửi lên cuộc gọi).

    spec: "test" (âm 440 Hz), hoặc URL/tệp ffmpeg đọc được. fmt "s16le16k": URL là PCM16 16 kHz thô (mic R1)."""
    out = f"-f s16le -ac {CHANNELS} -ar {SAMPLE_RATE} pipe:1"
    base = "ffmpeg -hide_banner -loglevel error -nostdin"
    if spec in ("", "test"):
        return f'{base} -re -f lavfi -i "sine=frequency=440:sample_rate={SAMPLE_RATE}" -af volume=0.25 {out}'
    src = shlex.quote(spec)
    if fmt == "s16le16k":
        return f"{base} -f s16le -ar 16000 -ac 1 -i {src} {out}"
    if spec.startswith(("rtsp://", "rtsps://")):
        return f"{base} -rtsp_transport tcp -i {src} -vn {out}"
    return f"{base} -i {src} -vn {out}"


VIDEO_W, VIDEO_H, VIDEO_FPS = 640, 360, 15


def camera_command(spec: str) -> str:
    """Lệnh shell xuất khung hình thô yuv420p 640x360 15 fps ra stdout (nguồn hình gửi lên cuộc gọi video)."""
    out = f"-vf scale={VIDEO_W}:{VIDEO_H},fps={VIDEO_FPS} -f rawvideo -pix_fmt yuv420p pipe:1"
    base = "ffmpeg -hide_banner -loglevel error -nostdin"
    if spec in ("", "test"):
        return f'{base} -re -f lavfi -i "testsrc2=size={VIDEO_W}x{VIDEO_H}:rate={VIDEO_FPS}" {out}'
    src = shlex.quote(spec)
    if spec.startswith(("rtsp://", "rtsps://")):
        return f"{base} -rtsp_transport tcp -i {src} -an {out}"
    return f"{base} -re -i {src} -an {out}"


class LiveCall:
    def __init__(self, runtime, phone: str, client, user_id: int, outgoing: bool, video: bool = False,
                 mic: str = "test", mic_fmt: str = "auto", cam: str = "test"):
        self.runtime, self.phone, self.client = runtime, phone, client
        self.user_id, self.outgoing, self.video = int(user_id), outgoing, video
        self.mic, self.mic_fmt, self.cam = mic, mic_fmt, cam
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
        self.playback_path = os.path.join(runtime.data_dir, "live", self.id + "_in.pcm")
        self.error = ""
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
                "frames_in": self.frames_in, "frame_bytes": self.frame_bytes, "peak": getattr(self, "peak", 0),
                "playback": {"kind": self.playback_kind, "bytes": self._playback_size()},
                "events": self.events[-40:]}

    def _playback_size(self) -> int:
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
                self._set("connecting", "TDLib: Ready (servers=%d, p2p=%s, emoji=%s)"
                          % (len(st.get("servers") or []), st.get("allow_p2p"), "".join(self.emojis)))
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
            cmd = mic_command(self.mic, self.mic_fmt)
            self.log("NTgCalls: mic = %s" % ("âm thử" if self.mic in ("", "test") else "nguồn ngoài"))
            cam = None
            if self.video:
                self.log("NTgCalls: camera = %s" % ("hình thử" if self.cam in ("", "test") else "nguồn ngoài"))
                cam = nt.VideoDescription(nt.MediaSource.SHELL, VIDEO_W, VIDEO_H, VIDEO_FPS, camera_command(self.cam), True)
            await ntg.set_stream_sources(self.user_id, nt.StreamMode.CAPTURE, nt.MediaDescription(
                nt.AudioDescription(nt.MediaSource.SHELL, SAMPLE_RATE, CHANNELS, cmd, True), None, cam, None))
            await self._set_playback(self.playback_kind)
            if m["encryption_key"]:
                self.log("NTgCalls: skip_exchange")
                await ntg.skip_exchange(self.user_id, m["encryption_key"], self.outgoing)
            self.log("NTgCalls: connect_p2p (%d máy chủ)" % len(servers))
            await ntg.connect_p2p(self.user_id, servers, m["versions"], m["p2p_allowed"], m["custom_parameters"])
            self.log("NTgCalls: connect_p2p xong")
        except Exception as exc:  # noqa: BLE001 - lỗi runtime phải thấy được ở nhật ký, không nuốt
            self.error = "%s: %s" % (type(exc).__name__, exc)
            self.log("NTgCalls LỖI " + self.error)
            logger.exception("live_call %s lỗi ở bước media", self.id)

    async def _set_playback(self, kind: str, cmd: str = "") -> None:
        """Cách nhận tiếng bên kia: external (khung về on_frames), file (NTgCalls ghi PCM thô vào tệp),
        shell (lệnh nhận PCM ở stdin). Đổi được ngay trong lúc gọi để thử."""
        nt = self.runtime.nt
        os.makedirs(os.path.dirname(self.playback_path), exist_ok=True)
        if kind == "external":
            src, inp = nt.MediaSource.EXTERNAL, ""
        elif kind == "shell":
            src = nt.MediaSource.SHELL
            inp = cmd or ("ffmpeg -hide_banner -loglevel error -f s16le -ar %d -ac %d -i pipe:0 -f wav %s.wav"
                          % (SAMPLE_RATE, CHANNELS, shlex.quote(self.playback_path)))
        else:
            kind, src, inp = "file", nt.MediaSource.FILE, self.playback_path
        self.playback_kind = kind
        self.log("NTgCalls: nhận tiếng bằng %s" % kind)
        await self.runtime.ntg.set_stream_sources(self.user_id, nt.StreamMode.PLAYBACK, nt.MediaDescription(
            None, nt.AudioDescription(src, SAMPLE_RATE, CHANNELS, inp, False), None, None))

    async def _send_signaling(self, data: bytes) -> None:
        try:
            await self.runtime.ntg.send_signaling_data(self.user_id, bytes(data))
        except Exception as exc:  # noqa: BLE001
            self.log("NTgCalls send_signaling_data lỗi: %s" % exc)

    async def _stop(self) -> None:
        try:
            await self.runtime.ntg.stop(self.user_id)
        except Exception as exc:  # noqa: BLE001 - chưa tạo cuộc gọi NTgCalls thì stop báo lỗi: bình thường
            self.log("NTgCalls stop: %s" % exc)

    # -- do người dùng ---------------------------------------------------------------------------------
    def hangup(self) -> None:
        if self.tdlib_call_id is not None and self.state != "ended":
            try:
                self.client.discard_call(self.tdlib_call_id)
            except Exception as exc:  # noqa: BLE001
                self.log("discardCall lỗi: %s" % exc)
        self._end("người dùng cúp")

    def accept(self) -> None:
        self.client.accept_call(self.tdlib_call_id, self.runtime.protocol())
        self.log("đã nghe máy")


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
        self._first_frame_logged = False

    def run(self, coro) -> None:
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        fut.add_done_callback(lambda f: f.exception() and logger.error("tác vụ NTgCalls lỗi: %r", f.exception()))

    def protocol(self) -> dict:
        p = self.nt.NTgCalls.get_protocol()
        return {"@type": "callProtocol", "udp_p2p": bool(p.udp_p2p), "udp_reflector": bool(p.udp_reflector),
                "min_layer": int(p.min_layer), "max_layer": int(p.max_layer),
                "library_versions": list(p.library_versions)}

    def by_user(self, user_id: int) -> "LiveCall | None":
        for c in self.calls.values():
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
        elif name in ("FAILED", "TIMEOUT", "CLOSED") and c.state != "ended":
            c._end("NTgCalls: %s" % name)
            self.on_event("ended", c)

    def _frames(self, user_id, mode, device, frames):
        c = self.by_user(user_id)
        if c is None:
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

    def _stream_end(self, user_id, stype, device):
        c = self.by_user(user_id)
        if c is not None:
            c.log("luồng kết thúc: %s/%s" % (getattr(stype, "name", stype), getattr(device, "name", device)))

    # -- API cho tầng REST -----------------------------------------------------------------------------
    def call_out(self, phone: str, client, user_id: int, video: bool = False, mic: str = "test",
                 mic_fmt: str = "auto", cam: str = "test", playback: str = "file") -> LiveCall:
        if self.by_user(user_id) is not None:
            raise RuntimeError("đang có cuộc gọi với người này")
        c = LiveCall(self, phone, client, user_id, True, video, mic, mic_fmt, cam)
        c.playback_kind = playback if playback in ("external", "file", "shell") else "file"
        self.calls[c.id] = c
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
            c = next((x for x in self.calls.values() if x.tdlib_call_id == cid), None)
            if c is None and call.get("is_outgoing"):
                # updateCall đầu tiên có thể tới TRƯỚC phản hồi createCall: nhận cuộc gọi đi chưa có mã, cùng người.
                c = next((x for x in self.calls.values() if x.outgoing and x.tdlib_call_id is None
                          and x.user_id == int(call.get("user_id") or 0) and x.state != "ended"), None)
                if c is not None:
                    c.tdlib_call_id = cid
            if c is None and not call.get("is_outgoing"):
                c = LiveCall(self, phone, client, call.get("user_id"), False, bool(call.get("is_video")))
                c.tdlib_call_id = cid
                self.calls[c.id] = c
                self.on_event("incoming", c)
            if c is not None:
                c.on_call_state(call)
                if c.state == "ended":
                    self.on_event("ended", c)
        elif t == "updateNewCallSignalingData":
            c = next((x for x in self.calls.values() if x.tdlib_call_id == obj.get("call_id")), None)
            if c is not None:
                raw = obj.get("data") or ""
                c.on_signaling(base64.b64decode(raw) if isinstance(raw, str) else bytes(raw))
