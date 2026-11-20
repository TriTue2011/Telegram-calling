"""Loa Google Cast (Google Home / Nest) làm LOA của cuộc gọi: tiếng bên kia → MP3 → loa, gateway tự điều khiển loa (không qua HA).

Địa chỉ loa nhận đúng hai dạng (cái khác bị từ chối — gateway không nối tới máy tuỳ ý):
  ``cast://<IPv4 nội bộ>``          ví dụ cast://192.168.1.50 (thử tay)
  ``cast://id/<mã Cast 32 hex>``    mã trong sổ thiết bị HA (định danh ``cast``); gateway tự tìm IP bằng mDNS (_googlecast._tcp)

Cách chạy (đo thật 20/11/2026 trên Google Home Mini bằng mic R1 + mic camera cùng phòng):
- Bộ nhận mặc định (Default Media Receiver, appId CC1AD845) KHÔNG phát luồng ``streamType: LIVE`` nguồn thời gian thực (loa câm,
  trạng thái BUFFERING mãi). Khai luồng như MỘT TỆP RẤT DÀI (``BUFFERED`` + ``Content-Length`` lớn) thì phát liên tục; trễ loa ≈ 3 s
  (bộ nhận tự giữ ~3 s đệm, cấp sẵn nhiều/ít không đổi được). Hợp nghe một chiều; nói chuyện qua lại sẽ chậm.
- NTgCalls đưa PCM vào lệnh ffmpeg (SHELL) → ffmpeg mã hoá MP3 và đẩy qua TCP 127.0.0.1 về :class:`CastStream` → phát cho loa qua
  ``GET /api/live/cast/<id>/<token>.mp3`` (tầng REST). Loa không gửi được Bearer nên đường này KHÔNG cần token gateway: thay bằng
  token ngẫu nhiên riêng từng cuộc gọi trong URL, chỉ sống khi cuộc gọi chưa kết thúc (ngoại lệ có chủ đích, có test).
- Bộ khách CASTV2 tối giản (TLS 8009, khung protobuf CastMessage tự mã hoá — không thêm thư viện): CONNECT → LAUNCH → LOAD,
  trả PONG/gửi PING mỗi 5 s, kết thúc thì STOP phiên (loa thôi phát, về trạng thái cũ).
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import re
import secrets
import socket
import ssl
import struct
import threading
import time

APP_ID = "CC1AD845"                     # Default Media Receiver
NS_CONN = "urn:x-cast:com.google.cast.tp.connection"
NS_HEART = "urn:x-cast:com.google.cast.tp.heartbeat"
NS_RECV = "urn:x-cast:com.google.cast.receiver"
NS_MEDIA = "urn:x-cast:com.google.cast.media"
CAST_PORT = 8009
BITRATE = 128_000                       # bit/s MP3
FAKE_LENGTH = BITRATE // 8 * 4 * 3600   # «tệp» 4 giờ: loa coi là BUFFERED và phát dần trong khi tải
PRIME_BYTES = BITRATE // 8              # ~1 s MP3 gần nhất giao ngay khi loa nối (đỡ chờ đầu)

_SPEC_RE = re.compile(r"^cast://(?:id/(?P<id>[0-9a-f]{32})|(?P<ip>[0-9.]{7,15}))$")


def parse_spec(spec: str) -> dict | None:
    """``{"id": …}`` hoặc ``{"ip": …}``; None nếu không đúng hai dạng cho phép (IP phải là IPv4 nội bộ)."""
    m = _SPEC_RE.match((spec or "").strip())
    if not m:
        return None
    if m.group("id"):
        return {"id": m.group("id")}
    try:
        ip = ipaddress.IPv4Address(m.group("ip"))
    except ValueError:
        return None
    if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return None
    return {"ip": str(ip)}


def is_cast_spec(spec: str) -> bool:
    return parse_spec(spec) is not None


def _mdns_query() -> bytes:
    q = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0)
    for part in ("_googlecast", "_tcp", "local"):
        q += bytes([len(part)]) + part.encode()
    return q + b"\x00" + struct.pack("!HH", 12, 1)


def resolve_id(cast_id: str, timeout: float = 3.0) -> str:
    """IP của loa có mã Cast ``cast_id`` (TXT ``id=`` trong phản hồi mDNS). Rỗng nếu không thấy."""
    want = ("id=" + cast_id).encode()
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.3)
        end, sent = time.time() + timeout, 0.0
        while time.time() < end:
            if time.time() - sent > 1.0:
                s.sendto(_mdns_query(), ("224.0.0.251", 5353))
                sent = time.time()
            try:
                data, addr = s.recvfrom(9000)
            except socket.timeout:
                continue
            if want in data.lower():
                return addr[0]
        return ""
    finally:
        s.close()


def local_ip_toward(ip: str) -> str:
    """IP của máy này trên đường tới ``ip`` (để loa kéo luồng về đúng địa chỉ LAN, kể cả khi gateway nghe 0.0.0.0)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((ip, CAST_PORT))
        return s.getsockname()[0]
    finally:
        s.close()


# -- khung CastMessage (protobuf) tự mã hoá -----------------------------------------------------------
def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def _field(tag: int, data: bytes) -> bytes:
    return bytes([tag]) + _varint(len(data)) + data


def encode_message(source: str, dest: str, namespace: str, payload: dict) -> bytes:
    body = (b"\x08\x00" + _field(0x12, source.encode()) + _field(0x1A, dest.encode()) + _field(0x22, namespace.encode())
            + b"\x28\x00" + _field(0x32, json.dumps(payload, separators=(",", ":")).encode()))
    return struct.pack(">I", len(body)) + body


def decode_message(body: bytes) -> dict:
    """Trả {source, dest, namespace, payload(dict|None)}; bỏ qua trường lạ."""
    out, i = {}, 0
    names = {2: "source", 3: "dest", 4: "namespace", 6: "payload"}
    while i < len(body):
        key = body[i]
        i += 1
        num, wt = key >> 3, key & 7
        if wt == 0:
            while body[i] & 0x80:
                i += 1
            i += 1
        elif wt == 2:
            ln, shift = 0, 0
            while True:
                b = body[i]
                i += 1
                ln |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            val = body[i:i + ln]
            i += ln
            if num in names:
                out[names[num]] = val.decode("utf-8", "replace")
        else:
            break
    try:
        out["payload"] = json.loads(out.get("payload") or "null")
    except ValueError:
        out["payload"] = None
    return out


class CastClient:
    """Một phiên phát trên loa: :meth:`play` (CONNECT/LAUNCH/LOAD, chạy nhịp tim trong luồng riêng), :meth:`stop`."""

    def __init__(self, ip: str, log=print, timeout: float = 8.0) -> None:
        self.ip, self.log, self.timeout = ip, log, timeout
        self._sock = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._req = 0
        self.session_id = ""
        self.transport_id = ""
        self.state = ""

    def _send(self, dest: str, ns: str, payload: dict) -> None:
        with self._lock:
            self._sock.sendall(encode_message("sender-0", dest, ns, payload))

    def _recv(self) -> dict:
        hdr = self._read_exact(4)
        return decode_message(self._read_exact(struct.unpack(">I", hdr)[0]))

    def _read_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("loa đóng kết nối Cast")
            buf += chunk
        return buf

    def _rid(self) -> int:
        self._req += 1
        return self._req

    def play(self, url: str, title: str = "Cuộc gọi") -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE            # loa Cast dùng chứng chỉ tự ký riêng từng máy; chỉ nối IP nội bộ đã kiểm
        raw = socket.create_connection((self.ip, CAST_PORT), timeout=self.timeout)
        self._sock = ctx.wrap_socket(raw)
        self._send("receiver-0", NS_CONN, {"type": "CONNECT"})
        self._send("receiver-0", NS_RECV, {"type": "LAUNCH", "appId": APP_ID, "requestId": self._rid()})
        end = time.time() + self.timeout
        while time.time() < end and not self.transport_id:
            m = self._recv()
            p = m.get("payload") or {}
            if m.get("namespace") == NS_HEART and p.get("type") == "PING":
                self._send(m.get("source") or "receiver-0", NS_HEART, {"type": "PONG"})
            for app in ((p.get("status") or {}).get("applications") or []) if p.get("type") == "RECEIVER_STATUS" else []:
                if app.get("appId") == APP_ID:
                    self.transport_id, self.session_id = app.get("transportId", ""), app.get("sessionId", "")
        if not self.transport_id:
            raise TimeoutError("loa không mở bộ phát mặc định")
        self._send(self.transport_id, NS_CONN, {"type": "CONNECT"})
        self._send(self.transport_id, NS_MEDIA, {
            "type": "LOAD", "requestId": self._rid(), "autoplay": True,
            "media": {"contentId": url, "streamType": "BUFFERED", "contentType": "audio/mpeg",
                      "metadata": {"metadataType": 0, "title": title}}})
        self._sock.settimeout(1.0)
        threading.Thread(target=self._loop, name="cast-heartbeat", daemon=True).start()

    def _loop(self) -> None:
        last_ping = 0.0
        while not self._stop.is_set():
            try:
                if time.time() - last_ping > 5:
                    self._send("receiver-0", NS_HEART, {"type": "PING"})
                    last_ping = time.time()
                m = self._recv()
            except socket.timeout:
                continue
            except (OSError, ConnectionError, ssl.SSLError) as exc:
                if not self._stop.is_set():
                    self.log("Cast: mất kết nối loa (%s)" % type(exc).__name__)
                return
            p = m.get("payload") or {}
            if m.get("namespace") == NS_HEART and p.get("type") == "PING":
                try:
                    self._send(m.get("source") or "receiver-0", NS_HEART, {"type": "PONG"})
                except OSError:
                    return
            elif m.get("namespace") == NS_MEDIA and p.get("type") == "MEDIA_STATUS":
                st = [s.get("playerState") for s in (p.get("status") or [])]
                if st and st[0] != self.state:
                    self.state = st[0]
                    self.log("Cast: loa %s" % self.state)

    def stop(self) -> None:
        self._stop.set()
        if self._sock is None:
            return
        try:
            if self.session_id:
                self._send("receiver-0", NS_RECV, {"type": "STOP", "sessionId": self.session_id, "requestId": self._rid()})
                time.sleep(0.2)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass


class CastStream:
    """Luồng MP3 của MỘT cuộc gọi: nhận byte MP3 từ ffmpeg qua TCP 127.0.0.1 (cổng ngẫu nhiên), giữ ~1 s gần nhất,
    phát cho người nghe (loa) đăng ký qua :meth:`subscribe`. Không có người nghe thì bỏ dữ liệu cũ — không bao giờ chặn ffmpeg."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(24)
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(2)
        self.port = self._srv.getsockname()[1]
        self._recent = bytearray()
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.closed = False
        self.bytes_in = 0
        threading.Thread(target=self._accept, name="cast-stream", daemon=True).start()

    def encoder_command(self) -> str:
        """Lệnh SHELL cho NTgCalls: PCM16 48 kHz mono ở stdin → MP3 CBR → TCP về luồng này (cố định, không nhận chuỗi ngoài)."""
        return ("ffmpeg -hide_banner -loglevel error -fflags nobuffer -flags low_delay -probesize 32 -analyzeduration 0 "
                "-f s16le -ar 48000 -ac 1 -i pipe:0 -c:a libmp3lame -b:a %dk -flush_packets 1 -f mp3 tcp://127.0.0.1:%d"
                % (BITRATE // 1000, self.port))

    def _accept(self) -> None:
        while not self.closed:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._read, args=(conn,), daemon=True).start()

    def _read(self, conn) -> None:
        with conn:
            while not self.closed:
                try:
                    b = conn.recv(4096)
                except OSError:
                    return
                if not b:
                    return
                self.bytes_in += len(b)
                with self._lock:
                    self._recent += b
                    del self._recent[:-PRIME_BYTES]
                    for q in self._subs:
                        try:
                            q.put_nowait(b)
                        except queue.Full:            # người nghe quá chậm: bỏ khúc (không dồn trễ)
                            pass

    def subscribe(self) -> tuple[queue.Queue, bytes]:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subs.append(q)
            return q, bytes(self._recent)

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def check(self, token: str) -> bool:
        return not self.closed and secrets.compare_digest(token or "", self.token)

    def close(self) -> None:
        self.closed = True
        try:
            self._srv.close()
        except OSError:
            pass
        with self._lock:
            for q in self._subs:
                try:
                    q.put_nowait(b"")
                except queue.Full:
                    pass


class CastSink:
    """Ghép :class:`CastStream` + :class:`CastClient` cho một cuộc gọi."""

    def __init__(self, spec: str, call_id: str, port: int, log=print) -> None:
        target = parse_spec(spec)
        if target is None:
            raise ValueError("speaker không phải cast://<IPv4 nội bộ> hoặc cast://id/<mã Cast>")
        self.target, self.call_id, self.port, self.log = target, call_id, port, log
        self.stream = CastStream()
        self.client: CastClient | None = None

    def start(self) -> None:
        """Tìm IP (nếu là mã), mở phiên Cast và cho loa kéo luồng. Chạy trong luồng nền; lỗi chỉ ghi nhật ký."""
        try:
            ip = self.target.get("ip") or resolve_id(self.target["id"])
            if not ip:
                self.log("Cast: không tìm thấy loa (mDNS) cho mã đã chọn")
                return
            host = local_ip_toward(ip)
            url = "http://%s:%d/api/live/cast/%s/%s.mp3" % (host, self.port, self.call_id, self.stream.token)
            self.client = CastClient(ip, log=self.log)
            self.client.play(url)
            self.log("Cast: đã mở bộ phát trên loa %s (trễ ≈ 3 s theo đo thật)" % ip)
        except Exception as exc:  # noqa: BLE001 - loa lỗi không được làm hỏng cuộc gọi
            self.log("Cast: không mở được loa: %s %s" % (type(exc).__name__, str(exc)[:120]))

    def close(self) -> None:
        if self.client is not None:
            self.client.stop()
        self.stream.close()
