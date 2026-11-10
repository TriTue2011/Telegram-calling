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
