"""Engine phương án B — P1/P2: TDLib báo hiệu + NTgCalls media.

Báo hiệu: TDLib (tdjson 1.8.69) — createCall / acceptCall / discardCall /
updateCall / sendCallSignalingData / updateNewCallSignalingData.
Media: NTgCalls 3.0.6 — entry point P2P + PCM vào/ra đã xác minh ở T0.

Glue THẬT (``map_ready`` + ``GlueSession``), theo map T0-B:
    servers            -> connect_p2p(servers: RTCServer)
    protocol           -> connect_p2p(versions, p2p_allowed)
    encryption_key     -> skip_exchange / exchange_keys
    custom_parameters  -> connect_p2p(custom_parameters)
    emojis             -> get_emojis_fingerprint
    signaling 2 chiều  -> TDLib sendCallSignalingData /
                          updateNewCallSignalingData
                          <-> NTgCalls send_signaling_data /
                          on_signaling_data

CAVEAT BINDING (từ T0-B): tdjson nhận bytes, không nhận str — request
đi qua ``tdclient.tdjson_bytes``; trường bytes trong JSON TDLib là
chuỗi base64 (bằng chứng probe T0-B). NTgCalls cũng nhận bytes cho
signaling.

TODO T1 — ĐỊNH DẠNG PCM GỌI RIÊNG TƯ CHƯA XÁC MINH: hằng
``ENGINE_PCM_FORMAT`` ở ``audio.py`` (s16le 48 kHz mono) chỉ là tiền lệ
group call; gọi riêng tư thật tài khoản phụ ↔ tài khoản test (T1) mới
chốt được. Đừng claim định dạng này đã đúng cho gọi riêng tư.

Các phương thức NTgCalls thật trả về awaitable (phải await trong ngữ
cảnh async); ``GlueSession`` trả lại awaitable đó cho caller, còn fake
ở test trả None nên test chạy đồng bộ bình thường.
"""

import base64
import os


class EngineNotReady(RuntimeError):
    pass


def _lazy_tdjson():
    import tdjson  # noqa: F401  (pin: tdjson==1.8.69)
    return tdjson


def _lazy_ntgcalls():
    import ntgcalls  # noqa: F401  (pin: ntgcalls==3.0.6)
    return ntgcalls


def _decode_bytes_field(value):
    """Trường bytes TDLib: bytes giữ nguyên, str base64 -> bytes."""
    if value is None:
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, str):
        try:
            return base64.b64decode(value)
        except Exception:
            return value.encode("utf-8")
    return b""


def map_ready(ready: dict) -> dict:
    """Map thuần ``callStateReady`` (TDLib) -> tham số NTgCalls.

    Fixture ở test dùng đúng hình JSON TDLib (``@type`` cho loại
    server, ``ip_address``/``ipv6_address``). Trả về dict có keys:
    servers (list dict khớp trường RTCServer), versions, p2p_allowed,
    encryption_key (bytes), custom_parameters (str|None), emojis.
    Hàm này thuần túy, không chạm mạng/engine — test được trực tiếp.
    """
    protocol = ready.get("protocol", {}) or {}
    versions = list(protocol.get("library_versions", []) or [])
    servers = []
    for srv in ready.get("servers", []) or []:
        stype = srv.get("type", {}) or {}
        stype_name = stype.get("@type", "") if isinstance(stype, dict) else ""
        is_reflector = "TelegramReflector" in stype_name or (
            isinstance(stype, dict) and "peer_tag" in stype)
        servers.append({
            "id": srv.get("id", 0),
            "ipv4": srv.get("ip_address", ""),
            "ipv6": srv.get("ipv6_address", ""),
            "port": srv.get("port", 0),
            "peer_tag": stype.get("peer_tag", "") if isinstance(stype, dict) else "",
            "turn": bool(stype.get("supports_turn", False)) if isinstance(stype, dict) else False,
            "stun": bool(stype.get("supports_stun", False)) if isinstance(stype, dict) else False,
            "tcp": bool(stype.get("is_tcp", False)) if isinstance(stype, dict) else False,
            "username": stype.get("username", "") if isinstance(stype, dict) else "",
            "password": stype.get("password", "") if isinstance(stype, dict) else "",
            "_reflector": is_reflector,
        })
    custom = ready.get("custom_parameters", "") or ""
    return {
        "servers": servers,
        "versions": versions,
        "p2p_allowed": bool(ready.get("allow_p2p", False)),
        "encryption_key": _decode_bytes_field(ready.get("encryption_key")),
        "custom_parameters": custom if custom else None,
        "emojis": list(ready.get("emojis", []) or []),
    }


class TdlibSignaling:
    """Vỏ quanh client TDLib cho một tài khoản.

    Khi ``client`` (TdlibClient hoặc fake cùng interface) được gắn,
    các lệnh đi thật qua TDLib. Khi chưa gắn, mọi lệnh raise
    EngineNotReady (hành vi khung P0, giữ nguyên cho test cũ).
    """

    def __init__(self, session_dir: str, client=None):
        self.session_dir = session_dir
        self.files_dir = os.path.join(session_dir, "files")
        self.key_path = os.path.join(session_dir, "khoa")
        self.client_id: int | None = None
        self._client = client
        if client is not None:
            self.client_id = getattr(client, "client_id", 1)

    def attach_client(self, client) -> None:
        self._client = client
        self.client_id = getattr(client, "client_id", 1)

    def ensure_client(self) -> None:
        if self._client is not None:
            return
        _lazy_tdjson()  # kiểm tra wheel có sẵn, chưa tạo client thật
        raise EngineNotReady(
            "Client TDLib chưa đăng nhập (chưa gắn client cho tài khoản).")

    def create_call(self, user_id: int, protocol: dict) -> int:
        self.ensure_client()
        return self._client.create_call(user_id, protocol)

    def accept_call(self, call_id: int, protocol: dict) -> None:
        self.ensure_client()
        return self._client.accept_call(call_id, protocol)

    def discard_call(self, call_id: int) -> None:
        self.ensure_client()
        return self._client.discard_call(call_id)

    def send_signaling_data(self, call_id: int, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("signaling data phải là bytes (binding tdjson).")
        self.ensure_client()
        return self._client.send_signaling_data(call_id, bytes(data))


class NtgCallsMedia:
    """Vỏ quanh NTgCalls cho media gọi 1-1.

    Khi ``engine`` được gắn (engine thật hoặc fake cùng interface),
    các lệnh ủy thác sang nó. Khi chưa gắn, raise EngineNotReady
    (hành vi khung P0, giữ nguyên cho test cũ).
    """

    def __init__(self, engine=None):
        self._engine = engine

    def ensure_engine(self) -> None:
        if self._engine is not None:
            return
        _lazy_ntgcalls()  # kiểm tra wheel có sẵn, chưa khởi động engine
        raise EngineNotReady(
            "Engine NTgCalls chưa khởi động (chưa gắn engine).")

    def create_p2p_call(self, chat_id: int):
        self.ensure_engine()
        return self._engine.create_p2p_call(chat_id)

    def connect_p2p(self, user_id: int, servers, versions,
                    p2p_allowed: bool, custom_parameters=None):
        self.ensure_engine()
        return self._engine.connect_p2p(user_id, servers, versions,
                                        p2p_allowed, custom_parameters)

    def skip_exchange(self, user_id: int, encryption_key: bytes,
                      is_outgoing: bool):
        self.ensure_engine()
        return self._engine.skip_exchange(user_id, encryption_key,
                                          is_outgoing)

    def send_signaling_data(self, chat_id: int, data: bytes):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("signaling data phải là bytes (NTgCalls).")
        self.ensure_engine()
        return self._engine.send_signaling_data(chat_id, bytes(data))

    def on_signaling_data(self, callback) -> None:
        self.ensure_engine()
        return self._engine.on_signaling_data(callback)

    def get_emojis_fingerprint(self, chat_id: int):
        self.ensure_engine()
        return self._engine.get_emojis_fingerprint(chat_id)


class RealNtgCallsMedia:
    """Adapter sang NTgCalls thật (lazy import, không import ở module load).

    Các phương thức trả lại đúng thứ engine trả (awaitable ở ntgcalls
    3.0.6) — caller async await nó; caller đồng bộ chỉ dùng để kiểm
    tra chữ ký. Việc chạy media thật với peer thật thuộc T1.
    """

    def __init__(self):
        ntgcalls = _lazy_ntgcalls()
        self._engine = ntgcalls.NTgCalls()

    def create_p2p_call(self, chat_id: int):
        return self._engine.create_p2p_call(chat_id)

    def connect_p2p(self, user_id: int, servers, versions,
                    p2p_allowed: bool, custom_parameters=None):
        ntgcalls = _lazy_ntgcalls()
        rtc_servers = []
        for s in servers:
            kwargs = dict(id=s.get("id", 0), ipv4=s.get("ipv4", ""),
                          ipv6=s.get("ipv6", ""), port=s.get("port", 0),
                          peer_tag=s.get("peer_tag", ""),
                          turn=s.get("turn", False),
                          stun=s.get("stun", False), tcp=s.get("tcp", False),
                          username=s.get("username", ""),
                          password=s.get("password", ""))
            try:
                rtc_servers.append(ntgcalls.RTCServer(**kwargs))
            except TypeError:
                # Binding pybind không nhận kwargs: dùng thứ tự vị trí
                # theo tài liệu pytgcalls (id, ipv4, ipv6, port, peer_tag,
                # turn, stun, tcp, username, password). T1 sẽ chốt.
                rtc_servers.append(ntgcalls.RTCServer(
                    kwargs["id"], kwargs["ipv4"], kwargs["ipv6"],
                    kwargs["port"], kwargs["peer_tag"], kwargs["turn"],
                    kwargs["stun"], kwargs["tcp"], kwargs["username"],
                    kwargs["password"]))
        return self._engine.connect_p2p(user_id, rtc_servers, versions,
                                        p2p_allowed, custom_parameters)

    def skip_exchange(self, user_id: int, encryption_key: bytes,
                      is_outgoing: bool):
        return self._engine.skip_exchange(user_id, encryption_key,
                                          is_outgoing)

    def send_signaling_data(self, chat_id: int, data: bytes):
        return self._engine.send_signaling_data(chat_id, bytes(data))

    def on_signaling_data(self, callback) -> None:
        return self._engine.on_signaling_data(callback)

    def get_emojis_fingerprint(self, chat_id: int):
        return self._engine.get_emojis_fingerprint(chat_id)


class GlueSession:
    """Nối một cuộc gọi: TDLib (báo hiệu) <-> NTgCalls (media).

    ``tdlib_call_id`` là id cuộc gọi phía TDLib (int32), ``user_id`` là
    id peer phía NTgCalls. Hai chiều signaling:
      TDLib updateNewCallSignalingData -> media.send_signaling_data
      NTgCalls on_signaling_data       -> tdlib.send_signaling_data
    """

    def __init__(self, signaling: TdlibSignaling, media: NtgCallsMedia,
                 user_id: int, is_outgoing: bool = True):
        self.signaling = signaling
        self.media = media
        self.user_id = user_id
        self.is_outgoing = is_outgoing
        self.tdlib_call_id: int | None = None
        self.last_mapped: dict | None = None
        self.emojis: list = []
        # Đăng ký chiều NTgCalls -> TDLib (fake ở test ghi lại callback).
        try:
            self.media.on_signaling_data(self._on_ntgcalls_signaling)
        except Exception:
            pass

    def bind_call(self, tdlib_call_id: int) -> None:
        self.tdlib_call_id = tdlib_call_id

    def on_ready(self, ready: dict):
        """Xử lý callStateReady: map -> skip_exchange -> connect_p2p.

        Trả về kết quả connect_p2p (awaitable với engine thật, None
        với fake) để caller async await khi cần.
        """
        mapped = map_ready(ready)
        self.last_mapped = mapped
        self.emojis = mapped["emojis"]
        if mapped["encryption_key"]:
            # TDLib đã thỏa thuận khóa -> bỏ qua trao đổi DH của engine.
            self.media.skip_exchange(self.user_id,
                                     mapped["encryption_key"],
                                     self.is_outgoing)
        result = self.media.connect_p2p(
            self.user_id, mapped["servers"], mapped["versions"],
            mapped["p2p_allowed"], mapped["custom_parameters"])
        return result

    def on_tdlib_signaling(self, data: bytes) -> None:
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("signaling data phải là bytes.")
        self.media.send_signaling_data(self.user_id, bytes(data))

    def _on_ntgcalls_signaling(self, chat_id: int, data: bytes) -> None:
        if self.tdlib_call_id is None:
            return
        self.signaling.send_signaling_data(self.tdlib_call_id,
                                            bytes(data))

    def handle_update(self, update: dict):
        """Định tuyến một update TDLib tới glue (gọi từ on_update)."""
        utype = update.get("@type", "")
        if utype == "updateNewCallSignalingData":
            data = _decode_bytes_field(update.get("data"))
            self.on_tdlib_signaling(data)
            return "signaling"
        if utype == "updateCall":
            call = update.get("call", {}) or {}
            state = call.get("state", {}) or {}
            if state.get("@type") == "callStateReady":
                self.bind_call(int(call.get("id", 0)))
                self.on_ready(state)
                return "ready"
        return None
