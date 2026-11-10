"""Cuộc gọi thật: lệnh nguồn mic/camera (rắc rối an toàn), chuyển trạng thái, nhận cuộc gọi đi tới sớm. Không mạng."""

import shlex

import pytest
from fastapi.testclient import TestClient

from gateway import live_call
from gateway.config import Config
from gateway.routes import build_app


class StubRuntime:
    data_dir = "/tmp/live-test"

    def __init__(self):
        self.ran = []

    def run(self, coro):
        coro.close()                       # không chạy NTgCalls thật
        self.ran.append(1)


class StubClient:
    def __init__(self):
        self.discarded = []
        self.accepted = []

    def discard_call(self, cid):
        self.discarded.append(cid)

    def accept_call(self, cid, protocol):
        self.accepted.append(cid)

    def create_call(self, uid, protocol, is_video=False):
        return 7


def test_mic_and_camera_commands_quote_untrusted_sources():
    evil = "http://h/x'; rm -rf / #"
    cmd = live_call.mic_command(evil)
    assert shlex.split(cmd)[-1] == "pipe:1" and evil in shlex.split(cmd)          # URL là MỘT đối số, không tách lệnh
    assert "rm -rf" not in cmd.replace(shlex.quote(evil), "")
    assert "sine=frequency=440" in live_call.mic_command("test")
    assert "-f s16le -ar 16000" in live_call.mic_command("http://r1/mic", "s16le16k")
    assert "-rtsp_transport tcp" in live_call.camera_command("rtsp://u:p@cam/s")
    assert "rawvideo" in live_call.camera_command("test") and "yuv420p" in live_call.camera_command("test")
    assert shlex.split(live_call.camera_command(evil))[-1] == "pipe:1"


def test_live_call_state_machine_and_hangup():
    rt, cl = StubRuntime(), StubClient()
    c = live_call.LiveCall(rt, "+84", cl, 5, True, False)
    c.tdlib_call_id = 7
    c.on_call_state({"state": {"@type": "callStatePending", "is_created": True, "is_received": False}})
    assert c.state == "dialing"
    c.on_call_state({"state": {"@type": "callStatePending", "is_created": True, "is_received": True}})
    assert c.state == "ringing"
    ready = {"@type": "callStateReady", "emojis": ["🚙", "🍚"], "servers": [], "allow_p2p": True}
    c.on_call_state({"state": ready})
    assert c.state == "connecting" and c.emojis == ["🚙", "🍚"] and len(rt.ran) == 1
    c.on_call_state({"state": ready})                      # Ready lặp lại: chỉ dựng media MỘT lần
    assert len(rt.ran) == 1
    c.hangup()
    assert cl.discarded == [7] and c.state == "ended"
    c.hangup()                                              # cúp hai lần không gửi discard lần nữa
    assert cl.discarded == [7]
    d = live_call.LiveCall(rt, "+84", cl, 5, True)
    d.on_call_state({"state": {"@type": "callStateDiscarded", "reason": {"@type": "callDiscardReasonDeclined"}}})
    assert d.state == "ended" and any("Declined" in e["e"] for e in d.events)
    e = live_call.LiveCall(rt, "+84", cl, 5, True)
    e.on_call_state({"state": {"@type": "callStateError", "error": {"code": 500, "message": "X"}}})
    assert e.state == "ended" and e.error == "X"
    assert len(c.info()["events"]) > 3 and "playback" in c.info()


def test_incoming_call_state_and_accept_uses_protocol():
    rt = StubRuntime()
    rt.protocol = lambda: {"@type": "callProtocol"}
    cl = StubClient()
    c = live_call.LiveCall(rt, "+84", cl, 9, False, True)
    c.tdlib_call_id = 11
    assert c.state == "ringing" and not c.outgoing and c.video
    c.accept()
    assert cl.accepted == [11]


def test_runtime_adopts_outgoing_call_when_update_arrives_before_createcall_returns():
    nt = pytest.importorskip("ntgcalls")
    rt = live_call.CallRuntime(data_dir="/tmp/live-test")
    cl = StubClient()
    c = live_call.LiveCall(rt, "+84", cl, 42, True)
    rt.calls[c.id] = c                                       # createCall chưa trả -> chưa có tdlib_call_id
    rt.on_tdlib_update("+84", cl, {"@type": "updateCall", "call": {
        "id": 3, "user_id": 42, "is_outgoing": True, "is_video": False,
        "state": {"@type": "callStatePending", "is_created": True, "is_received": False}}})
    assert c.tdlib_call_id == 3 and c.state == "dialing"
    events = []
    rt.on_event = lambda kind, call: events.append(kind)
    rt.on_tdlib_update("+84", cl, {"@type": "updateCall", "call": {
        "id": 4, "user_id": 99, "is_outgoing": False, "is_video": True,
        "state": {"@type": "callStatePending", "is_created": True, "is_received": True}}})
    assert events == ["incoming"]
    inc = [x for x in rt.calls.values() if x.user_id == 99][0]
    assert not inc.outgoing and inc.video and inc.state == "ringing"
    p = rt.protocol()
    assert p["@type"] == "callProtocol" and p["min_layer"] > 0 and p["library_versions"]


def _client(tmp_path):
    cfg = Config(data_dir=str(tmp_path), gateway_token="tk")
    return TestClient(build_app(cfg), raise_server_exceptions=False)


def test_live_settings_validate_mask_and_persist(tmp_path):
    c = _client(tmp_path)
    H = {"Authorization": "Bearer tk"}
    assert c.get("/api/live/settings").status_code == 401
    assert c.get("/api/live/settings", headers=H).json()["mic"] == "test"
    assert c.post("/api/live/settings", headers=H, json={"mic": "ftp://x"}).status_code == 400
    assert c.post("/api/live/settings", headers=H, json={"camera": "rtsp://u:secret@cam/s", "mic": "http://r1/mic?k=1",
                                                       "mic_format": "s16le16k"}).json() == {"ok": True}
    got = c.get("/api/live/settings", headers=H).json()
    assert got["camera"] == "rtsp://u:***@cam/s" and "secret" not in str(got) and got["mic_format"] == "s16le16k"
    # Gửi lại bản đã che không ghi đè mật khẩu thật.
    c.post("/api/live/settings", headers=H, json={"camera": got["camera"]})
    import json, os
    saved = json.load(open(os.path.join(str(tmp_path), "live_settings.json")))
    assert saved["camera"] == "rtsp://u:secret@cam/s"
    assert oct(os.stat(os.path.join(str(tmp_path), "live_settings.json")).st_mode & 0o777) == "0o600"
    assert c.post("/api/live/hangup", headers=H, json={"id": "x"}).status_code == 404


# ── Loa nhận tiếng bên kia: kiểu phát «push» ───────────────────────────────────────────────────────

_R1_URL = "http://172.16.10.200:8123/api/r1_speaker/intercom/01M61K2CMCVTZKHGG3TWR3976N?k=ab-cd_EF%20x"
_DUPLEX_URL = "https://ha.local/api/dahua_talk/duplex/ABC123?k=tok"


def test_speaker_url_validation_and_fixed_command():
    for u in (_R1_URL, _DUPLEX_URL):
        assert live_call.valid_speaker_url(u), u
    bad = ["", "ftp://h/api/r1_speaker/intercom/E?k=1", "http://h/api/other/intercom/E?k=1", "http://h/api/r1_speaker/intercom/E",
           "http://h/api/r1_speaker/intercom/E?k=1;rm -rf /", "http://h/api/r1_speaker/intercom/E?k=1 &x",
           "http://h/api/r1_speaker/mic/E?k=1", "http://h x/api/r1_speaker/intercom/E?k=1",
           "http://h/api/r1_speaker/intercom/E?k=$(id)", "http://h/api/r1_speaker/intercom/../E?k=1",
           "http://h/api/r1_speaker/intercom/E?k=1\nfoo"]
    for u in bad:
        assert not live_call.valid_speaker_url(u), u
        with pytest.raises(ValueError):
            live_call.speaker_command(u)
    a = shlex.split(live_call.speaker_command(_R1_URL))
    assert a[-1] == _R1_URL and "pcm_alaw" in a and a[a.index("-method") + 1] == "POST"      # URL là MỘT đối số
    assert a[a.index("-ar") + 1] == "48000" and "pipe:0" in a                                  # đầu vào PCM 48 kHz từ stdin
    d = shlex.split(live_call.speaker_command(_DUPLEX_URL))
    assert "pcm_s16le" in d and "pcm_alaw" not in d and d[d.index("-c:a") + 1] == "pcm_s16le"  # song công: PCM16 16 kHz thô


def test_live_settings_speaker_validate_mask_and_persist(tmp_path):
    import json, os
    c = _client(tmp_path)
    H = {"Authorization": "Bearer tk"}
    assert c.get("/api/live/settings", headers=H).json()["speaker"] == ""
    assert c.post("/api/live/settings", headers=H, json={"speaker": "http://evil/x"}).status_code == 400
    assert c.post("/api/live/settings", headers=H, json={"speaker": _R1_URL, "mic": "http://r1/mic?k=SECRETMIC"}).json() == {"ok": True}
    got = c.get("/api/live/settings", headers=H).json()
    assert got["speaker"].endswith("?k=***") and "ab-cd" not in str(got) and "SECRETMIC" not in str(got), got
    c.post("/api/live/settings", headers=H, json={"speaker": got["speaker"], "mic": got["mic"]})      # gửi lại bản đã che
    path = os.path.join(str(tmp_path), "live_settings.json")
    saved = json.load(open(path))
    assert saved["speaker"] == _R1_URL and saved["mic"] == "http://r1/mic?k=SECRETMIC"
    assert c.post("/api/live/settings", headers=H, json={"speaker": ""}).json() == {"ok": True}      # rỗng = bỏ loa
    assert json.load(open(path))["speaker"] == ""


def test_call_out_with_speaker_uses_push_and_rejects_bad_url():
    pytest.importorskip("ntgcalls")
    rt = live_call.CallRuntime(data_dir="/tmp/live-test")
    cl = StubClient()
    c = rt.call_out("+84", cl, 5, speaker_url=_R1_URL)
    assert c.playback_kind == "push" and c.speaker_url == _R1_URL
    assert "ab-cd" not in str(c.info()), "địa chỉ loa (có khoá) không được lộ ra info/nhật ký"
    with pytest.raises(ValueError):
        rt.call_out("+84", cl, 6, speaker_url="http://evil/x")


def test_set_playback_push_builds_fixed_command_for_ntgcalls():
    import asyncio
    seen = []

    class NT:
        class MediaSource:
            SHELL, FILE, EXTERNAL = "SHELL", "FILE", "EXT"

        class StreamMode:
            PLAYBACK = "PB"

        @staticmethod
        def AudioDescription(*a):
            return ("AD",) + a

        @staticmethod
        def MediaDescription(*a):
            return ("MD",) + a

    class NTG:
        async def set_stream_sources(self, uid, mode, desc):
            seen.append(desc)

    rt = StubRuntime()
    rt.nt, rt.ntg = NT, NTG()
    c = live_call.LiveCall(rt, "+84", StubClient(), 5, True)
    asyncio.run(c._set_playback("push", _R1_URL))
    ad = seen[0][2]                                  # MediaDescription(None, AudioDescription(...), None, None)
    assert ad[1] == "SHELL" and ad[4] == live_call.speaker_command(_R1_URL) and c.playback_kind == "push"
    with pytest.raises(ValueError):
        asyncio.run(c._set_playback("push", "http://evil/x"))


def test_incoming_call_gets_default_devices_before_accept():
    rt = StubRuntime()
    c = live_call.LiveCall(rt, "+84", StubClient(), 9, False, True)
    assert c.mic == "test" and c.cam == "test"                  # giá trị khởi tạo của cuộc gọi đến
    live_call.apply_defaults(c, {"mic": "http://r1/mic?k=1", "mic_format": "s16le16k", "camera": "rtsp://cam/s",
                                 "speaker": _R1_URL, "playback": "file"})
    assert (c.mic, c.mic_fmt, c.cam) == ("http://r1/mic?k=1", "s16le16k", "rtsp://cam/s")
    assert c.playback_kind == "push" and c.speaker_url == _R1_URL
    live_call.apply_defaults(c, {"mic": "test", "mic_format": "auto", "camera": "test", "speaker": "", "playback": "external"})
    assert c.playback_kind == "external" and c.speaker_url == ""
    live_call.apply_defaults(c, {"mic": "test", "mic_format": "auto", "camera": "test", "speaker": "http://evil/x", "playback": "shell"})
    assert c.playback_kind == "file" and c.speaker_url == ""     # địa chỉ loa sai / kiểu shell không được áp từ mặc định


def test_integration_intercom_url_is_accepted_by_gateway():
    """Hợp đồng giữa hai nửa của kho: địa chỉ cửa bộ đàm mà tích hợp telegram_call SINH RA phải qua kiểm tra của gateway
    (và địa chỉ có khoá lạ thì gateway vẫn từ chối). Nạp bridge_core.py của tích hợp bằng đường dẫn (không cần Home Assistant)."""
    import importlib.util
    import os
    path = os.path.join(os.path.dirname(__file__), "..", "..", "custom_components", "telegram_call", "bridge_core.py")
    spec = importlib.util.spec_from_file_location("tc_bridge_core", path)
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    for domain in ("r1_speaker", "dahua_talk"):
        for kind in ("intercom", "duplex"):
            for entry_id, key in (("01M61K2CMCVTZKHGG3TWR3976N", "ab-cd_EF123"), ("abc123", "k e/y;&"), ("E-1_x", "z" * 43)):
                u = core.intercom_url("http://172.16.10.200:8123/", domain, entry_id, key, kind)
                assert live_call.valid_speaker_url(u), u
                live_call.speaker_command(u)                                  # không ném lỗi


def test_gateway_version_comes_from_version_file(tmp_path):
    import os
    want = open(os.path.join(os.path.dirname(__file__), "..", "VERSION"), encoding="utf-8").read().strip()
    assert want and Config(data_dir=str(tmp_path), gateway_token="tk").version == want
    c = _client(tmp_path)
    assert c.get("/health").json()["version"] == want
