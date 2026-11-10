"""Test khung gateway Docker — mức mock, không mạng, không tài khoản thật."""

import os
import stat

import pytest
import schema
from fastapi.testclient import TestClient

from gateway.audio import AudioHub
from gateway.calls import CallManager
from gateway.config import Config
from gateway.engine import EngineNotReady, NtgCallsMedia, TdlibSignaling
from gateway.events import EventBridge
from gateway.redact import RedactingFilter, mask_peer, setup_logging
from gateway.routes import build_app
from gateway.store import AccountStore


def make_client(tmp_path, rate_limit=120):
    cfg = Config(data_dir=str(tmp_path), gateway_token="test-token",
                 rate_limit_per_minute=rate_limit)
    app = build_app(cfg)
    return TestClient(app, raise_server_exceptions=False), app, cfg


def auth():
    return {"Authorization": "Bearer test-token"}


def test_health_no_auth(tmp_path):
    client, _, _ = make_client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in ("ok", "degraded")
    assert body["mock_mode"] is True
    assert "test-token" not in r.text


def test_auth_required_and_rate_limit(tmp_path):
    client, app, _ = make_client(tmp_path, rate_limit=3)
    assert client.get("/api/accounts").status_code == 401
    assert client.get("/api/accounts",
                      headers={"Authorization": "Bearer sai"}).status_code == 401
    for _ in range(3):
        assert client.get("/api/accounts", headers=auth()).status_code == 200
    r = client.get("/api/accounts", headers=auth())
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "RATE_LIMITED"


def test_accounts_read_only_no_secrets(tmp_path):
    client, app, _ = make_client(tmp_path)
    store = app.state.store
    store.create_session("+84000000001", "Phu")
    r = client.get("/api/accounts", headers=auth())
    assert r.status_code == 200
    assert r.json()["tai_khoan"][0]["phone"] == "+84000000001"
    # Giá trị khóa session thật không được lộ qua API.
    key_path = os.path.join(store.session_dir("+84000000001"), "khoa")
    with open(key_path, encoding="utf-8") as fh:
        assert fh.read() not in r.text
    assert "test-token" not in r.text


def test_send_message_mock_and_validation(tmp_path):
    client, app, _ = make_client(tmp_path)
    store = app.state.store
    store.create_session("+84000000001")
    # Thiếu peer/message -> 400
    assert client.post("/api/send_message", headers=auth(),
                       json={"peer": "x"}).status_code == 400
    # Tài khoản không rõ -> 404
    assert client.post("/api/send_message", headers=auth(),
                       json={"account": "+84999999999", "peer": "x",
                             "message": "hi"}).status_code == 404
    r = client.post("/api/send_message", headers=auth(),
                    json={"peer": "nguoi_nhan", "message": "chao"})
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["mock"] is True
    # Event đã spool, poll lấy được
    ev = client.get("/api/events?since=0", headers=auth()).json()
    assert any(e["type"] == "message_new" for e in ev["events"])


def test_stage_501_endpoints(tmp_path):
    client, app, _ = make_client(tmp_path)
    for method, path in [("POST", "/api/send_media"),
                         ("POST", "/api/send_voice")]:
        r = client.request(method, path, headers=auth(), json={})
        assert r.status_code == 501, path
        assert "giai đoạn" in r.json()["error"]["message"]


def test_unknown_route_404_wrong_method_405(tmp_path):
    client, app, _ = make_client(tmp_path)
    assert client.get("/api/duong_la", headers=auth()).status_code == 404
    assert client.post("/health", headers=auth()).status_code == 405


def test_body_not_dict_400(tmp_path):
    client, app, _ = make_client(tmp_path)
    r = client.post("/api/send_message", headers=auth(), json=["khong-phai-dict"])
    assert r.status_code == 400


def test_call_flow_busy_queue_promotion(tmp_path):
    client, app, _ = make_client(tmp_path)
    store = app.state.store
    store.create_session("+84000000001")
    # Gọi 1: trả ngay dialing
    r1 = client.post("/api/call", headers=auth(),
                     json={"peer": "a", "mode": "tts", "tts_text": "canh bao"})
    assert r1.status_code == 200
    assert r1.json()["status"] == "dialing"
    call1 = r1.json()["call_id"]
    # Gọi 2 cùng tài khoản, không queue -> 409 ACCOUNT_BUSY
    r2 = client.post("/api/call", headers=auth(),
                     json={"peer": "b", "mode": "live"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "ACCOUNT_BUSY"
    # Gọi 3 có queue -> vào hàng đợi (vẫn trả dialing, queued)
    r3 = client.post("/api/call", headers=auth(),
                     json={"peer": "c", "mode": "live", "queue": True,
                           "audio_endpoint": "usb"})
    assert r3.status_code == 200
    call3 = r3.json()["call_id"]
    # Nghe máy + cúp máy cuộc 1 -> cuộc 3 được đưa lên
    assert client.post("/api/answer_call", headers=auth(),
                       json={"call_id": call1}).json()["state"] == "active"
    assert client.post("/api/hangup_call", headers=auth(),
                       json={"call_id": call1}).json()["state"] == "ended"
    calls = client.get("/api/calls", headers=auth()).json()["calls"]
    by_id = {c["call_id"]: c for c in calls}
    assert by_id[call3]["state"] == "dialing"
    # Cúp cuộc không tồn tại -> 409 CALL_NOT_ACTIVE
    r = client.post("/api/hangup_call", headers=auth(),
                    json={"call_id": "khong-co"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CALL_NOT_ACTIVE"


def test_endpoint_busy_across_accounts(tmp_path):
    client, app, _ = make_client(tmp_path)
    store = app.state.store
    store.create_session("+84000000001")
    store.create_session("+84000000002")
    r1 = client.post("/api/call", headers=auth(),
                     json={"account": "+84000000001", "peer": "a",
                           "mode": "live", "audio_endpoint": "ha"})
    assert r1.status_code == 200
    r2 = client.post("/api/call", headers=auth(),
                     json={"account": "+84000000002", "peer": "b",
                           "mode": "live", "audio_endpoint": "ha"})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "ENDPOINT_BUSY"


def test_login_ui_mock_and_flood_lock(tmp_path):
    client, app, _ = make_client(tmp_path)
    r = client.post("/ui/api/accounts", headers=auth(),
                    json={"phone": "+84000000001"})
    assert r.status_code == 200
    login_id = r.json()["login_id"]
    # 2 mã sai định dạng -> 400
    for _ in range(2):
        assert client.post(f"/ui/api/accounts/{login_id}/code",
                           headers=auth(), json={"code": "abc"}).status_code == 400
    # Mã sai thứ 3 -> khóa 429 LOGIN_LOCKED
    r = client.post(f"/ui/api/accounts/{login_id}/code",
                    headers=auth(), json={"code": "xyz"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "LOGIN_LOCKED"
    # Login mới, mã đúng định dạng -> tạo session mock
    login2 = client.post("/ui/api/accounts", headers=auth(),
                         json={"phone": "+84000000002"}).json()["login_id"]
    r = client.post(f"/ui/api/accounts/{login2}/code",
                    headers=auth(), json={"code": "123456"})
    assert r.status_code == 200
    assert r.json()["mock"] is True


def test_session_durable_across_restart_and_permissions(tmp_path):
    store1 = AccountStore(str(tmp_path))
    store1.create_session("+84000000001", "Phu")
    key_path = os.path.join(store1.session_dir("+84000000001"), "khoa")
    with open(key_path, encoding="utf-8") as fh:
        key1 = fh.read()
    # "Restart": tạo lại store trên cùng thư mục dữ liệu
    store2 = AccountStore(str(tmp_path))
    acc = store2.get("+84000000001")
    assert acc is not None and acc.display_name == "Phu"
    with open(key_path, encoding="utf-8") as fh:
        assert fh.read() == key1  # khóa còn nguyên
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    files_dir = os.path.join(store2.session_dir("+84000000001"), "files")
    assert stat.S_IMODE(os.stat(files_dir).st_mode) == 0o700
    assert store2.remove("+84000000001") is True
    assert store2.get("+84000000001") is None


def test_event_cursor_monotonic_and_poll(tmp_path):
    bridge = EventBridge(str(tmp_path))
    e1 = bridge.emit("message_new", "+84000000001", {"x": 1})
    e2 = bridge.emit("call_ended", "+84000000001", {"x": 2})
    assert e2["id"] == e1["id"] + 1
    out = bridge.poll(e1["id"])
    assert [e["id"] for e in out["events"]] == [e2["id"]]
    assert out["cursor"] == e2["id"]


def test_log_redaction(tmp_path):
    logger = setup_logging("INFO", secrets=("test-token", "123456"))
    record_filter = logger.handlers[0].filters[0]
    import logging
    rec = logging.LogRecord("t", logging.INFO, __file__, 1,
                            "token=test-token code=123456 Bearer abc",
                            (), None)
    record_filter.filter(rec)
    assert "test-token" not in rec.msg
    assert "123456" not in rec.msg
    assert "••••" in rec.msg
    assert mask_peer("+84000000001") not in ["+84000000001"]


def test_engine_not_ready_and_bytes_rule(tmp_path):
    sig = TdlibSignaling(str(tmp_path))
    with pytest.raises(EngineNotReady):
        sig.create_call(123, {})
    with pytest.raises(TypeError):
        sig.send_signaling_data(1, "khong-phai-bytes")
    media = NtgCallsMedia()
    with pytest.raises(EngineNotReady):
        media.create_p2p_call(123)


def test_audiohub_claim_release():
    hub = AudioHub()
    assert hub.is_free("ha") is True
    assert hub.claim("ha", "call-1") is True
    assert hub.claim("ha", "call-2") is False
    hub.release("ha", "call-1")
    assert hub.is_free("ha") is True


# ===================== P1 + P2 (thêm mới, fake client/engine) ==============
import base64

from gateway.audio import (FileSink, FileSource, FrameQueue, PipeSink,
                           PipeSource, R1StreamSource, UsbMicSource)
from gateway.calls import CallManager
from gateway.engine import (GlueSession, NtgCallsMedia, TdlibSignaling,
                            map_ready)
from gateway.events import EventBridge
from gateway.tdclient import TelegramError
from gateway.tts import TtsUploadStore


class FakeTdlibClient:
    """Fake cùng interface TdlibClient (không mạng, không tài khoản thật)."""

    def __init__(self, phone, session_dir, correct_code="12345",
                 require_password=False, qr_link="https://t.me/qr-fake",
                 flood_code=False, flood_phone=False):
        self.phone = phone
        self.session_dir = session_dir
        self.client_id = None
        self.correct_code = correct_code
        self.require_password = require_password
        self.qr_link = qr_link
        self.flood_code = flood_code
        self.flood_phone = flood_phone
        self._password_done = False
        self._qr_done = False
        self.sent = []
        self.signaling_sent = []
        self.closed = False

    def open(self):
        self.client_id = 99

    def close(self):
        self.closed = True
        self.client_id = None

    def set_phone(self, phone):
        if self.flood_phone:
            raise TelegramError(429, "FLOOD_WAIT_120")
        self.phone = phone

    def check_code(self, code):
        if self.flood_code:
            raise TelegramError(429, "FLOOD_WAIT_60")
        if code != self.correct_code:
            raise TelegramError(400, "PHONE_CODE_INVALID")
        if self.require_password and not self._password_done:
            return "password"
        return "ok"

    def check_password(self, password):
        if password != "pw-ok":
            raise TelegramError(400, "PASSWORD_HASH_INVALID")
        self._password_done = True
        return "ok"

    def request_qr(self):
        return self.qr_link

    def refresh_auth(self):
        return ("authorizationStateReady" if self._qr_done
                else "authorizationStateWaitOtherDeviceConfirmation"), self.qr_link

    def is_authorized(self):
        return self._qr_done

    def get_me(self):
        return {"first_name": "Phu", "last_name": "Test"}

    def send_message(self, peer, text):
        self.sent.append(("message", peer, text))
        return 1001

    def send_media(self, peer, file_path, caption=""):
        self.sent.append(("media", peer, file_path, caption))
        return 1002

    def send_voice(self, peer, audio_path, duration=0):
        self.sent.append(("voice", peer, audio_path))
        return 1003

    def create_call(self, user_id, protocol):
        return 555

    def accept_call(self, call_id, protocol):
        return None

    def discard_call(self, call_id):
        return None

    def send_signaling_data(self, call_id, data):
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("signaling data phải là bytes.")
        self.signaling_sent.append((call_id, bytes(data)))


class FakeTdlibFactory:
    def __init__(self, **kw):
        self.kw = kw
        self.created = []

    def __call__(self, phone, session_dir):
        c = FakeTdlibClient(phone, session_dir, **self.kw)
        self.created.append(c)
        return c


class FakeNtgCalls:
    def __init__(self):
        self.connected = []
        self.skipped = []
        self.signaling_sent = []
        self._cb = None

    def create_p2p_call(self, chat_id):
        return None

    def connect_p2p(self, user_id, servers, versions, p2p_allowed,
                    custom_parameters=None):
        self.connected.append({"user_id": user_id, "servers": servers,
                               "versions": versions,
                               "p2p_allowed": p2p_allowed,
                               "custom_parameters": custom_parameters})
        return None

    def skip_exchange(self, user_id, encryption_key, is_outgoing):
        self.skipped.append((user_id, encryption_key, is_outgoing))
        return None

    def send_signaling_data(self, chat_id, data):
        self.signaling_sent.append((chat_id, bytes(data)))
        return None

    def on_signaling_data(self, callback):
        self._cb = callback

    def get_emojis_fingerprint(self, chat_id):
        return ["emoji-a", "emoji-b"]

    def fire_signaling(self, chat_id, data):
        if self._cb is not None:
            self._cb(chat_id, data)


class FakeSynthesizer:
    def synthesize(self, text, voice="", rate=""):
        return b"\x07\x08" * 1000


class BoomSynthesizer:
    def synthesize(self, text, voice="", rate=""):
        raise RuntimeError("edge-tts chưa cài (giả lập lỗi engine).")


def make_client_p1(tmp_path, factory=None, synthesizer=None):
    cfg = Config(data_dir=str(tmp_path), gateway_token="test-token")
    app = build_app(cfg, tdlib_factory=factory,
                    synthesizer=synthesizer)
    return TestClient(app, raise_server_exceptions=False), app, cfg


READY_FIXTURE = {
    "protocol": {"udp_p2p": True, "udp_reflector": True,
                 "min_layer": 65, "max_layer": 92,
                 "library_versions": ["1.0", "2.0"]},
    "servers": [
        {"id": 1, "ip_address": "1.2.3.4", "ipv6_address": "",
         "port": 1234,
         "type": {"@type": "callServerTypeTelegramReflector",
                  "peer_tag": "aabbccdd", "is_tcp": False}},
        {"id": 2, "ip_address": "5.6.7.8", "ipv6_address": "",
         "port": 443,
         "type": {"@type": "callServerTypeWebrtc", "username": "u",
                  "password": "p", "supports_turn": True,
                  "supports_stun": True}},
    ],
    "config": "{}",
    "encryption_key": base64.b64encode(b"\x11" * 32).decode("ascii"),
    "emojis": ["😀", "😁", "😂", "🤣"],
    "allow_p2p": True,
    "custom_parameters": '{"x": 1}',
}


def test_p1_login_real_otp_ok_and_send_real(tmp_path):
    factory = FakeTdlibFactory()
    client, app, _ = make_client_p1(tmp_path, factory=factory)
    login_id = client.post("/ui/api/accounts", headers=auth(),
                           json={"phone": "+84000000001"}).json()["login_id"]
    r = client.post(f"/ui/api/accounts/{login_id}/code", headers=auth(),
                    json={"code": "12345"})
    assert r.status_code == 200
    assert r.json()["status"] == "online"
    assert r.json()["mock"] is False
    accs = client.get("/api/accounts", headers=auth()).json()["tai_khoan"]
    assert accs[0]["status"] == "online"
    # Gửi thật qua client đã gắn (không còn mock).
    r = client.post("/api/send_message", headers=auth(),
                    json={"peer": "nguoi_nhan", "message": "chao that"})
    assert r.status_code == 200
    assert r.json()["mock"] is False
    assert r.json()["tin_id"] == "1001"
    assert factory.created[0].sent[0][0] == "message"


def test_p1_login_wrong_code_lock(tmp_path):
    factory = FakeTdlibFactory()
    client, _, _ = make_client_p1(tmp_path, factory=factory)
    login_id = client.post("/ui/api/accounts", headers=auth(),
                           json={"phone": "+84000000001"}).json()["login_id"]
    # Mã đúng định dạng nhưng server từ chối -> tính là thử sai.
    for _ in range(2):
        r = client.post(f"/ui/api/accounts/{login_id}/code", headers=auth(),
                        json={"code": "99999"})
        assert r.status_code == 400
    r = client.post(f"/ui/api/accounts/{login_id}/code", headers=auth(),
                    json={"code": "99999"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "LOGIN_LOCKED"
    # Đang khóa: mã đúng cũng vẫn 429.
    r = client.post(f"/ui/api/accounts/{login_id}/code", headers=auth(),
                    json={"code": "12345"})
    assert r.status_code == 429


def test_p1_login_2fa_flow(tmp_path):
    factory = FakeTdlibFactory(require_password=True)
    client, _, _ = make_client_p1(tmp_path, factory=factory)
    login_id = client.post("/ui/api/accounts", headers=auth(),
                           json={"phone": "+84000000001"}).json()["login_id"]
    r = client.post(f"/ui/api/accounts/{login_id}/code", headers=auth(),
                    json={"code": "12345"})
    assert r.status_code == 200
    assert r.json()["needs_password"] is True
    # 2FA sai -> 400.
    assert client.post(f"/ui/api/accounts/{login_id}/password",
                       headers=auth(),
                       json={"password": "sai"}).status_code == 400
    r = client.post(f"/ui/api/accounts/{login_id}/password", headers=auth(),
                    json={"password": "pw-ok"})
    assert r.status_code == 200
    assert r.json()["status"] == "online"


def test_p1_login_qr_flow(tmp_path):
    factory = FakeTdlibFactory()
    client, app, _ = make_client_p1(tmp_path, factory=factory)
    r = client.post("/ui/api/accounts/qr", headers=auth(),
                    json={"phone": "+84000000001"})
    assert r.status_code == 200
    login_id = r.json()["login_id"]
    assert r.json()["qr_link"] == "https://t.me/qr-fake"
    # Chưa quét -> 400.
    assert client.post(f"/ui/api/accounts/{login_id}/qr-confirm",
                       headers=auth(), json={}).status_code == 400
    # Giả lập quét xong rồi xác nhận.
    app.state.logins._pending[login_id]["client"]._qr_done = True
    r = client.post(f"/ui/api/accounts/{login_id}/qr-confirm",
                    headers=auth(), json={})
    assert r.status_code == 200
    assert r.json()["status"] == "online"


def test_p1_login_flood_wait_from_telegram(tmp_path):
    factory = FakeTdlibFactory(flood_code=True)
    client, _, _ = make_client_p1(tmp_path, factory=factory)
    login_id = client.post("/ui/api/accounts", headers=auth(),
                           json={"phone": "+84000000001"}).json()["login_id"]
    r = client.post(f"/ui/api/accounts/{login_id}/code", headers=auth(),
                    json={"code": "12345"})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "FLOOD_WAIT"
    # Số giây lấy thẳng từ Telegram (60), gateway không tự bịa.
    assert r.json()["error"]["retry_after_seconds"] == 60


def test_p1_send_media_voice_real(tmp_path):
    factory = FakeTdlibFactory()
    client, _, _ = make_client_p1(tmp_path, factory=factory)
    login_id = client.post("/ui/api/accounts", headers=auth(),
                           json={"phone": "+84000000001"}).json()["login_id"]
    client.post(f"/ui/api/accounts/{login_id}/code", headers=auth(),
                json={"code": "12345"})
    r = client.post("/api/send_media", headers=auth(),
                    json={"peer": "p", "media_b64": base64.b64encode(
                        b"fake-bytes").decode(), "filename": "a.bin"})
    assert r.status_code == 200
    assert r.json()["tin_id"] == "1002" and r.json()["mock"] is False
    r = client.post("/api/send_voice", headers=auth(),
                    json={"peer": "p", "audio_b64": base64.b64encode(
                        b"OggS-fake-ogg").decode()})                 # đầu OggS: đã là ogg nên không qua ffmpeg
    assert r.status_code == 200
    assert r.json()["tin_id"] == "1003"


class AsyncioRunSynthesizer:
    """Như EdgeTtsSynthesizer thật: tổng hợp đồng bộ nhưng bên trong gọi asyncio.run."""
    def synthesize(self, text, voice="", rate=""):
        import asyncio

        async def _go():
            return b"\x01\x02" * 500
        return asyncio.run(_go())


def test_p1_send_voice_tts_text_runs_synthesizer_off_the_event_loop(tmp_path):
    """Gọi synthesizer thẳng trong vòng lặp FastAPI làm «asyncio.run() cannot be called from a running event loop»."""
    factory = FakeTdlibFactory()
    client, _, _ = make_client_p1(tmp_path, factory=factory, synthesizer=AsyncioRunSynthesizer())
    login_id = client.post("/ui/api/accounts", headers=auth(), json={"phone": "+84000000001"}).json()["login_id"]
    client.post(f"/ui/api/accounts/{login_id}/code", headers=auth(), json={"code": "12345"})
    r = client.post("/api/send_voice", headers=auth(), json={"peer": "p", "tts_text": "xin chao"})
    assert r.status_code == 200, r.text
    assert r.json()["mock"] is False


def test_p1_no_login_endpoint_under_api(tmp_path):
    client, app, _ = make_client(tmp_path)
    api_paths = [getattr(rt, "path", "") for rt in app.routes
                 if getattr(rt, "path", "").startswith("/api/")]
    for path in api_paths:
        lowered = path.lower()
        assert "login" not in lowered and "password" not in lowered
        assert "/code" not in lowered and "/qr" not in lowered
    assert client.post("/api/login", headers=auth(),
                       json={}).status_code == 404


def test_p1_session_durable_real_login(tmp_path):
    factory = FakeTdlibFactory()
    client, app, _ = make_client_p1(tmp_path, factory=factory)
    login_id = client.post("/ui/api/accounts", headers=auth(),
                           json={"phone": "+84000000001"}).json()["login_id"]
    client.post(f"/ui/api/accounts/{login_id}/code", headers=auth(),
                json={"code": "12345"})
    store = app.state.store
    key_path = os.path.join(store.session_dir("+84000000001"), "khoa")
    with open(key_path, encoding="utf-8") as fh:
        key1 = fh.read()
    store2 = AccountStore(str(tmp_path))
    acc = store2.get("+84000000001")
    assert acc is not None and acc.status == "online"
    with open(key_path, encoding="utf-8") as fh:
        assert fh.read() == key1
    assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(
        os.path.join(store2.session_dir("+84000000001"),
                     "files")).st_mode) == 0o700
    assert "test-token" not in client.get(
        "/api/accounts", headers=auth()).text


def test_p2_call_ringing_queue_priority_and_timeout(tmp_path):
    cm = CallManager(queue_depth=10, queue_timeout=120)
    c1 = cm.start_call("acc", "a", "live")
    assert cm.mark_ringing(c1.call_id).state == "ringing"
    assert cm.answer(c1.call_id).state == "active"
    normal = cm.start_call("acc", "b", "live", queue=True,
                           priority="normal")
    alarm = cm.start_call("acc", "c", "live", queue=True,
                          priority="alarm")
    assert normal.queued is True and alarm.queued is True
    cm.hangup(c1.call_id)
    # Ưu tiên alarm > normal: cuộc alarm được đưa lên trước.
    assert cm.last_promoted is not None
    assert cm.last_promoted.call_id == alarm.call_id
    # Hết hạn hàng đợi: cuộc normal già -> prune thành queue_timeout.
    import time as _t
    for item in cm._queues["acc"]:
        item.enqueued_at = _t.time() - 9999
    cm.prune_expired_queue()
    assert cm.get(normal.call_id).state == "ended"
    assert cm.get(normal.call_id).reason == "queue_timeout"


def test_p2_glue_mapping_fixture():
    mapped = map_ready(READY_FIXTURE)
    assert mapped["versions"] == ["1.0", "2.0"]
    assert mapped["p2p_allowed"] is True
    assert mapped["encryption_key"] == b"\x11" * 32
    assert mapped["custom_parameters"] == '{"x": 1}'
    assert mapped["emojis"] == ["😀", "😁", "😂", "🤣"]
    reflector, webrtc = mapped["servers"]
    assert reflector["ipv4"] == "1.2.3.4" and reflector["peer_tag"] == "aabbccdd"
    assert webrtc["turn"] is True and webrtc["stun"] is True
    assert webrtc["username"] == "u" and webrtc["password"] == "p"


def test_p2_glue_session_bidirectional(tmp_path):
    fake_td = FakeTdlibClient("+84000000001", str(tmp_path))
    fake_td.open()
    sig = TdlibSignaling(str(tmp_path), client=fake_td)
    fake_media = FakeNtgCalls()
    media = NtgCallsMedia(engine=fake_media)
    glue = GlueSession(sig, media, user_id=777, is_outgoing=True)
    glue.bind_call(555)
    glue.on_ready(READY_FIXTURE)
    assert fake_media.skipped[0][1] == b"\x11" * 32
    assert len(fake_media.connected) == 1
    assert fake_media.connected[0]["custom_parameters"] == '{"x": 1}'
    assert fake_media.connected[0]["versions"] == ["1.0", "2.0"]
    # Chiều TDLib -> NTgCalls (updateNewCallSignalingData, base64).
    glue.handle_update({"@type": "updateNewCallSignalingData",
                        "call_id": 555,
                        "data": base64.b64encode(b"\xaa\xbb").decode()})
    assert fake_media.signaling_sent == [(777, b"\xaa\xbb")]
    # Chiều NTgCalls -> TDLib.
    fake_media.fire_signaling(777, b"\xcc\xdd")
    assert fake_td.signaling_sent == [(555, b"\xcc\xdd")]
    # updateCall callStateReady cũng định tuyến được qua handle_update.
    glue2 = GlueSession(sig, media, user_id=778)
    out = glue2.handle_update(
        {"@type": "updateCall",
         "call": {"id": 556,
                  "state": {"@type": "callStateReady", **READY_FIXTURE}}})
    assert out == "ready"
    assert glue2.tdlib_call_id == 556


def test_p2_tts_call_and_upload_cleanup(tmp_path):
    client, app, _ = make_client_p1(tmp_path,
                                    synthesizer=FakeSynthesizer())
    store = app.state.store
    store.create_session("+84000000001")
    # TTS từ text: bộ đệm phát có PCM fake, cúp máy thì hết.
    r = client.post("/api/call", headers=auth(),
                    json={"peer": "a", "mode": "tts",
                          "tts_text": "canh bao"})
    assert r.status_code == 200
    call_id = r.json()["call_id"]
    assert app.state.audio.playback_remaining(call_id) == 2000
    assert client.post("/api/hangup_call", headers=auth(),
                       json={"call_id": call_id}).status_code == 200
    assert app.state.audio.playback_remaining(call_id) == 0
    # TTS của HA qua upload theo khối -> dùng -> xóa sau cuộc gọi.
    up = client.post("/api/audio/upload", headers=auth(),
                     json={"chunk_b64": base64.b64encode(
                         b"HA-AUDIO").decode(), "done": True}).json()
    tts_audio_id = up["tts_audio_id"]
    assert app.state.tts_uploads.get_path(tts_audio_id) is not None
    r = client.post("/api/call", headers=auth(),
                    json={"peer": "b", "mode": "tts",
                          "tts_audio_id": tts_audio_id})
    assert r.status_code == 200
    call2 = r.json()["call_id"]
    assert client.post("/api/hangup_call", headers=auth(),
                       json={"call_id": call2}).status_code == 200
    assert app.state.tts_uploads.get_path(tts_audio_id) is None


def test_p2_tts_synthesizer_failure_502(tmp_path):
    client, app, _ = make_client_p1(tmp_path,
                                    synthesizer=BoomSynthesizer())
    app.state.store.create_session("+84000000001")
    r = client.post("/api/call", headers=auth(),
                    json={"peer": "a", "mode": "tts",
                          "tts_text": "xin chao"})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "MEDIA_ENGINE_ERROR"
    calls = client.get("/api/calls", headers=auth()).json()["calls"]
    assert calls and calls[0]["state"] == "ended"


def test_p2_audio_in_framing_missing_and_backpressure(tmp_path):
    client, app, _ = make_client(tmp_path)
    app.state.store.create_session("+84000000001")
    call_id = client.post("/api/call", headers=auth(),
                          json={"peer": "a", "mode": "live"}).json()["call_id"]

    def push(seq, payload):
        return client.post("/api/audio/in", headers=auth(),
                           json={"call_id": call_id, "seq": seq,
                                 "pcm_b64": base64.b64encode(
                                     payload).decode(),
                                 "sample_rate": 48000, "channels": 1})

    assert push(0, b"A" * 100).status_code == 200
    assert push(1, b"B" * 100).status_code == 200
    assert push(3, b"D" * 100).status_code == 200  # mất khối seq 2
    hub = app.state.audio
    assert hub.pop_in(call_id) == b"A" * 100
    assert hub.pop_in(call_id) == b"B" * 100
    silence = hub.pop_in(call_id)
    assert silence == b"\x00" * FrameQueue.CHUNK_BYTES
    assert hub.pop_in(call_id) == b"D" * 100
    assert hub.inbound_stats(call_id)["missing_silence"] == 1
    # Backpressure ở tầng FrameQueue (hàng đợi đầy).
    fq = FrameQueue(max_chunks=2)
    assert fq.push(0, b"x") == "ok"
    assert fq.push(1, b"x") == "ok"
    assert fq.push(2, b"x") == "backpressure"
    assert fq.stats()["backpressure"] == 1
    # Khối quá cũ -> stale, đếm.
    fq2 = FrameQueue()
    fq2.push(0, b"x")
    fq2.pop()
    for seq in range(1, 15):
        fq2.push(seq, b"x")
        fq2.pop()
    assert fq2.push(0, b"x") == "stale"
    assert fq2.stats()["dropped_stale"] == 1
    # Route audio/in: cuộc không tồn tại -> 409 CALL_NOT_ACTIVE.
    r = client.post("/api/audio/in", headers=auth(),
                    json={"call_id": "khong-co", "seq": 0,
                          "pcm_b64": base64.b64encode(b"x").decode()})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CALL_NOT_ACTIVE"


def test_p2_audio_in_route_backpressure_429(tmp_path):
    client, app, _ = make_client(tmp_path)
    app.state.store.create_session("+84000000001")
    call_id = client.post("/api/call", headers=auth(),
                          json={"peer": "a", "mode": "live"}).json()["call_id"]
    payload = base64.b64encode(b"x" * 10).decode()
    for seq in range(50):  # lấp đầy FrameQueue mặc định (50 khối)
        assert client.post("/api/audio/in", headers=auth(),
                           json={"call_id": call_id, "seq": seq,
                                 "pcm_b64": payload}).status_code == 200
    r = client.post("/api/audio/in", headers=auth(),
                    json={"call_id": call_id, "seq": 50,
                          "pcm_b64": payload})
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "BACKPRESSURE"


def test_p2_pipe_and_file_roundtrip(tmp_path):
    src = PipeSource()
    sink = PipeSink()
    src.start()
    sink.start()
    src.feed(b"hello-pcm")
    chunk = src.read_chunk()
    assert chunk == b"hello-pcm"
    sink.write(chunk)
    assert sink.drain() == b"hello-pcm"
    assert src.read_chunk() == b""  # hết khối, không chặn
    src.stop()
    sink.stop()
    pcm_path = os.path.join(str(tmp_path), "mic.pcm")
    out_path = os.path.join(str(tmp_path), "loa.pcm")
    with open(pcm_path, "wb") as fh:
        fh.write(b"\x01\x02" * 6000)
    fsrc = FileSource(pcm_path)
    fsink = FileSink(out_path)
    fsrc.start()
    fsink.start()
    fsink.write(fsrc.read_chunk())
    fsrc.stop()
    fsink.stop()
    with open(out_path, "rb") as fh:
        assert fh.read() == b"\x01\x02" * 4800  # FileSource đọc 9600 byte


def test_p2_event_webhook_retry_then_spool_and_restart(tmp_path):
    attempts = []

    def failing_sender(url, body, token):
        attempts.append(url)
        raise ConnectionError("HA không tới được")

    bridge = EventBridge(str(tmp_path), webhook_url="http://ha/hook",
                         webhook_token="tok", sender=failing_sender)
    ev = bridge.emit("message_new", "+84000000001", {"x": 1})
    # Webhook thử 3 lần rồi spool: event vẫn lấy được qua poll.
    assert len(attempts) == 3
    assert bridge.poll(0)["events"][0]["id"] == ev["id"]
    # Restart: cursor khôi phục đơn điệu từ spool, không quay lại.
    bridge2 = EventBridge(str(tmp_path))
    assert bridge2.cursor == ev["id"]
    ev2 = bridge2.emit("call_ended", "+84000000001", {"x": 2})
    assert ev2["id"] == ev["id"] + 1


def test_p2_r1_usb_interface_only():
    with pytest.raises(RuntimeError):
        R1StreamSource().start()
    with pytest.raises(RuntimeError):
        UsbMicSource().start()


def test_p2_dispatch_error_codes_full(tmp_path):
    client, app, _ = make_client(tmp_path)
    # Chưa có tài khoản -> gọi 503 ACCOUNT_OFFLINE.
    r = client.post("/api/call", headers=auth(),
                    json={"peer": "a", "mode": "live"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "ACCOUNT_OFFLINE"
    app.state.store.create_session("+84000000001")
    # tts_audio_id không tồn tại -> 404.
    r = client.post("/api/call", headers=auth(),
                    json={"peer": "a", "mode": "tts",
                          "tts_audio_id": "khong-co"})
    assert r.status_code == 404
    # upload_id không tồn tại -> 404; chunk base64 hỏng -> 400.
    r = client.post("/api/audio/upload", headers=auth(),
                    json={"upload_id": "khong-co", "chunk_b64": "aGVsbG8=",
                          "done": True})
    assert r.status_code == 404
    r = client.post("/api/audio/upload", headers=auth(),
                    json={"chunk_b64": "!!!khong-phai-base64!!!",
                          "done": False})
    assert r.status_code == 400
    # TTS upload store: prune file quá 1 giờ.
    store = TtsUploadStore(str(tmp_path))
    out = store.add_chunk(None, base64.b64encode(b"old").decode(),
                          done=True)
    path = store.get_path(out["tts_audio_id"])
    os.utime(path, (0, 0))
    assert store.prune_expired(ttl=3600) >= 1
    assert store.get_path(out["tts_audio_id"]) is None


def test_root_redirects_to_ui_page(tmp_path):
    client, _, _ = make_client(tmp_path)
    r = client.get("/", follow_redirects=False)
    assert r.status_code in (301, 302, 307, 308)
    assert r.headers["location"] == "/ui/app.html"


class _Td1869Transport:
    """Giả lập ĐÚNG hành vi TDLib 1.8.69 đã đo trên thư viện thật: setTdlibParameters chỉ đọc
    trường PHẲNG (bọc trong "parameters" thì api_id = 0 và trả lỗi), không có bước khóa mã hoá."""

    def __init__(self):
        self.sent = []
        self._queue = []

    def create_client_id(self):
        return 1

    def execute(self, data):
        return None

    def send(self, client_id, data):
        import json as _j
        req = _j.loads(data.decode("utf-8"))
        schema.validate(req)
        self.sent.append(req)
        extra = req.get("@extra")
        if req["@type"] == "setTdlibParameters":
            if not req.get("api_id") or not req.get("api_hash"):
                self._queue.append({"@type": "error", "code": 400, "@extra": extra,
                                    "message": "Valid api_id must be provided. Can be obtained at https://my.telegram.org"})
            else:
                self._queue.append({"@type": "ok", "@extra": extra})
                self._queue.append({"@type": "updateAuthorizationState", "authorization_state":
                                    {"@type": "authorizationStateWaitPhoneNumber"}})
        elif req["@type"] == "getAuthorizationState":
            self._queue.append({"@type": "authorizationStateWaitPhoneNumber", "@extra": extra})
        else:
            self._queue.append({"@type": "ok", "@extra": extra})

    def receive(self, timeout):
        import json as _j
        return _j.dumps(self._queue.pop(0)).encode("utf-8") if self._queue else None


def test_tdlib_client_open_sends_flat_parameters_with_real_api_id(tmp_path):
    from gateway.tdclient import TdlibClient
    tr = _Td1869Transport()
    c = TdlibClient(str(tmp_path), 12345678, "a" * 32, transport=tr)
    c.open()                                    # nếu còn lồng "parameters" thì ném TelegramError
    first = next(r for r in tr.sent if r["@type"] == "setTdlibParameters")
    assert first["api_id"] == 12345678 and first["api_hash"] == "a" * 32
    assert "parameters" not in first and "database_encryption_key" in first
    assert not any(r["@type"] == "checkDatabaseEncryptionKey" for r in tr.sent)
    assert c.authorization_state() == "authorizationStateWaitPhoneNumber"


def test_tdlib_client_open_with_real_tdjson_reaches_phone_number_state(tmp_path):
    """Chạy với thư viện TDLib THẬT (không mạng): api_id/api_hash giả hợp lệ về định dạng."""
    pytest.importorskip("tdjson")
    from gateway.tdclient import TdlibClient
    c = TdlibClient(str(tmp_path), 1, "0" * 32)
    c.open()
    assert c.authorization_state() == "authorizationStateWaitPhoneNumber"
    c.close()


def test_qr_status_refreshes_link_and_finishes_when_scanned(tmp_path):
    client, app, _ = make_client_p1(tmp_path, factory=FakeTdlibFactory())
    r = client.post("/ui/api/accounts/qr", headers=auth(), json={"phone": "+84900000011"})
    assert r.status_code == 200, r.text
    lid = r.json()["login_id"]
    fake = app.state.logins._pending[lid]["client"]
    fake.qr_link = "https://t.me/qr-2"                       # Telegram đổi mã sau ~30 giây
    st = client.get(f"/ui/api/accounts/{lid}", headers=auth()).json()
    assert st["stage"] == "qr" and st["qr_link"] == "https://t.me/qr-2"
    fake._qr_done = True                                     # đã quét xong -> tự hoàn tất, không cần nút
    st = client.get(f"/ui/api/accounts/{lid}", headers=auth()).json()
    assert st["stage"] == "done"
    assert lid not in app.state.logins._pending
    assert any(a["phone"] == "+84900000011" for a in
               client.get("/api/accounts", headers=auth()).json()["tai_khoan"])


def test_new_login_for_same_phone_closes_the_abandoned_client(tmp_path):
    """Lỗi thật: bấm QR rồi «Gửi mã» -> client cũ giữ khóa td.binlog -> "Can't lock file"."""
    client, app, _ = make_client_p1(tmp_path, factory=FakeTdlibFactory())
    r = client.post("/ui/api/accounts/qr", headers=auth(), json={"phone": "+84900000012"})
    old = app.state.logins._pending[r.json()["login_id"]]["client"]
    assert old.closed is False
    r2 = client.post("/ui/api/accounts", headers=auth(), json={"phone": "+84900000012"})
    assert r2.status_code == 200, r2.text
    assert old.closed is True                                # đã đóng trước khi mở client mới
    assert len(app.state.logins._pending) == 1


def test_stale_pending_logins_are_swept_and_closed(tmp_path):
    from gateway import login as login_mod
    client, app, _ = make_client_p1(tmp_path, factory=FakeTdlibFactory())
    r = client.post("/ui/api/accounts/qr", headers=auth(), json={"phone": "+84900000013"})
    lid = r.json()["login_id"]
    old = app.state.logins._pending[lid]["client"]
    app.state.logins._pending[lid]["created"] -= login_mod.PENDING_TTL + 5
    client.post("/ui/api/accounts", headers=auth(), json={"phone": "+84900000014"})   # bất kỳ lần bắt đầu mới
    assert lid not in app.state.logins._pending and old.closed is True


def test_failed_start_closes_its_client(tmp_path):
    client, app, _ = make_client_p1(tmp_path, factory=FakeTdlibFactory())
    # Không có hàm mở lỗi trong fake mặc định: kiểm đường close khi _discard trực tiếp.
    lid = app.state.logins.start("+84900000015")
    c = app.state.logins._pending[lid]["client"]
    app.state.logins._discard(lid)
    assert c.closed is True and lid not in app.state.logins._pending


def test_real_tdjson_second_client_on_same_dir_needs_the_first_to_be_closed(tmp_path):
    """Đo trên TDLib thật: mở client thứ hai trên cùng thư mục khi cái đầu CHƯA đóng -> lỗi khóa
    td.binlog (đúng lỗi người dùng gặp); đóng cái đầu (chờ đóng hẳn) rồi mới mở thì được."""
    pytest.importorskip("tdjson")
    from gateway.tdclient import TdlibClient
    d = str(tmp_path)
    a = TdlibClient(d, 1, "0" * 32)
    a.open()
    b = TdlibClient(d, 1, "0" * 32)
    with pytest.raises(Exception) as ei:
        b.open()
    assert "lock" in str(ei.value).lower()
    b.close()
    a.close()                                   # chờ authorizationStateClosed
    c = TdlibClient(d, 1, "0" * 32)
    c.open()
    assert c.authorization_state() == "authorizationStateWaitPhoneNumber"
    c.close()


class _QrTransport(_Td1869Transport):
    """TDLib 1.8.69: sau requestQrCodeAuthentication báo authorizationStateWaitOtherDeviceConfirmation
    (KHÔNG còn WaitQrCode); link đổi định kỳ."""

    def send(self, client_id, data):
        import json as _j
        req = _j.loads(data.decode("utf-8"))
        schema.validate(req)
        if req["@type"] == "requestQrCodeAuthentication":
            self.sent.append(req)
            self._queue.append({"@type": "ok", "@extra": req["@extra"]})
            self._queue.append({"@type": "updateAuthorizationState", "authorization_state": {
                "@type": "authorizationStateWaitOtherDeviceConfirmation", "link": "tg://login?token=AAA"}})
        else:
            super().send(client_id, data)

    def new_link(self, link):
        self._queue.append({"@type": "updateAuthorizationState", "authorization_state": {
            "@type": "authorizationStateWaitOtherDeviceConfirmation", "link": link}})


def test_request_qr_reads_link_from_wait_other_device_confirmation_and_follows_refresh(tmp_path):
    """Lỗi thật: gateway chỉ nhận tên bước cũ WaitQrCode -> link rỗng -> trang vẽ QR của chuỗi rỗng."""
    from gateway.tdclient import TdlibClient
    tr = _QrTransport()
    c = TdlibClient(str(tmp_path), 12345678, "a" * 32, transport=tr)
    c.open()
    link = c.request_qr()
    assert link == "tg://login?token=AAA"
    tr.new_link("tg://login?token=BBB")             # Telegram đổi mã sau ~30 giây
    state, link2 = c.refresh_auth()
    assert link2 == "tg://login?token=BBB"


def test_start_qr_rejects_empty_link(tmp_path):
    client, app, _ = make_client_p1(tmp_path, factory=FakeTdlibFactory(qr_link=""))
    r = client.post("/ui/api/accounts/qr", headers=auth(), json={"phone": "+84900000021"})
    assert r.status_code == 400 and "QR" in r.text
    assert app.state.logins.pending_count() == 0


def test_real_tdlib_returns_a_scannable_qr_link(tmp_path):
    """Cần mạng và TELEGRAM_API_ID/HASH thật (có trong container gateway); không có thì bỏ qua."""
    pytest.importorskip("tdjson")
    api_id, api_hash = os.environ.get("TELEGRAM_API_ID"), os.environ.get("TELEGRAM_API_HASH")
    if not (api_id and api_hash):
        pytest.skip("không có TELEGRAM_API_ID/HASH")
    from gateway.tdclient import TdlibClient
    c = TdlibClient(str(tmp_path), int(api_id), api_hash)
    c.open()
    link = c.request_qr(timeout=30)
    c.close()
    assert link.startswith("tg://login?token="), "link QR rỗng hoặc sai dạng"
