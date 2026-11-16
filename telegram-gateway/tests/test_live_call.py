"""Cuộc gọi thật: lệnh nguồn mic/camera (rắc rối an toàn), chuyển trạng thái, nhận cuộc gọi đi tới sớm. Không mạng."""

import shlex
import time

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

    def discard_call(self, cid, duration=0, is_video=False):
        self.discarded.append(cid)
        self.discard_args = {"duration": duration, "is_video": is_video}

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


def test_ended_call_deletes_far_end_audio_file_and_keeps_size(tmp_path):
    """Kiểu `file` ghi ~96 KB/giây; xoá khi kết thúc, nhưng info còn báo kích thước đã nhận."""
    import asyncio
    rt = StubRuntime()
    rt.data_dir = str(tmp_path)
    rt.ntg = type("N", (), {"stop": staticmethod(lambda uid: _done())})()
    c = live_call.LiveCall(rt, "+84", StubClient(), 5, True)
    (tmp_path / "live").mkdir()
    for p in (c.playback_path, c.playback_path + ".wav"):
        open(p, "wb").write(b"x" * 1000)
    assert c.info()["playback"]["bytes"] == 1000
    asyncio.run(c._stop())
    import os
    assert not os.path.exists(c.playback_path) and not os.path.exists(c.playback_path + ".wav")
    assert c.info()["playback"]["bytes"] == 1000


async def _done():
    return None


def test_runtime_prunes_old_ended_calls_but_never_active_ones():
    rt = object.__new__(live_call.CallRuntime)
    rt.calls = {}
    for i in range(live_call.MAX_ENDED_CALLS + 7):
        c = live_call.LiveCall(StubRuntime(), "+84", StubClient(), 100 + i, True)
        c.state, c.ended_at = "ended", float(i + 1)
        rt.calls[c.id] = c
    live = live_call.LiveCall(StubRuntime(), "+84", StubClient(), 1, True)      # đang chạy: không được đụng tới
    rt.calls[live.id] = live
    rt._prune()
    ended = [x for x in rt.calls.values() if x.state == "ended"]
    assert len(ended) == live_call.MAX_ENDED_CALLS and live.id in rt.calls
    assert min(x.ended_at for x in ended) == 8.0, "phải bỏ cái cũ nhất trước"



def test_video_profile_low_for_weak_machines_and_unknown_falls_back():
    assert live_call.video_profile({}) == (640, 360, 15)
    assert live_call.video_profile({"VIDEO_PROFILE": " Low "}) == (480, 270, 10)
    assert live_call.video_profile({"VIDEO_PROFILE": "gì-đó"}) == (640, 360, 15)
    w, h, fps = live_call.video_profile({"VIDEO_PROFILE": "low"})
    assert w % 2 == 0 and h % 2 == 0, "yuv420p cần kích thước chẵn"


def _td(responses):
    """TdlibClient giả: chỉ có call() trả lời theo @type, để thử resolve_user_id không cần TDLib thật."""
    from gateway.tdclient import TdlibClient
    cl = object.__new__(TdlibClient)
    seen = []
    cl.call = lambda req: (seen.append(req), responses[req["@type"]])[1]
    return cl, seen


def test_resolve_user_id_accepts_id_username_and_phone():
    from gateway.tdclient import TelegramError
    cl, seen = _td({"searchPublicChat": {"@type": "chat", "id": 7, "type": {"@type": "chatTypePrivate", "user_id": 7}},
                    "searchUserByPhoneNumber": {"@type": "user", "id": 99}})
    assert cl.resolve_user_id("123456789") == 123456789 and seen == []          # số thuần = ID, không hỏi TDLib
    assert cl.resolve_user_id("@ben_bap") == 7 and seen[-1]["username"] == "ben_bap"
    assert cl.resolve_user_id("ben_bap") == 7                                   # tên trần cũng được
    assert cl.resolve_user_id("+84 912 345 678") == 99 and seen[-1]["phone_number"] == "84912345678"
    with pytest.raises(ValueError):
        cl.resolve_user_id("+84abc")
    with pytest.raises(ValueError):
        cl.resolve_user_id("  ")
    chan, _ = _td({"searchPublicChat": {"@type": "chat", "id": -100, "type": {"@type": "chatTypeSupergroup", "supergroup_id": 5}}})
    with pytest.raises(TelegramError) as e:
        chan.resolve_user_id("@mot_kenh")                                         # kênh/nhóm không gọi được
    assert e.value.code == 404
    nob, _ = _td({"searchUserByPhoneNumber": {"@type": "ok"}})
    with pytest.raises(TelegramError):
        nob.resolve_user_id("+84900000000")


def test_sweep_stale_audio_runs_without_a_call_runtime(tmp_path):
    d = tmp_path / "live"
    d.mkdir()
    (d / "x_in.pcm").write_bytes(b"1")
    live_call.sweep_stale_audio(str(tmp_path))
    assert list(d.iterdir()) == []
    live_call.sweep_stale_audio(str(tmp_path / "khong-co"))                      # thư mục chưa có: không nổ


# ── Đổi nguồn gửi đi giữa cuộc gọi (người nhà nói trực tiếp, tắt mic, tắt hình) ─────────────────────────

def test_mic_silence_command_and_settings_accept_silence(tmp_path):
    assert "anullsrc" in live_call.mic_command("silence") and "sine" not in live_call.mic_command("silence")
    c = _client(tmp_path)
    H = {"Authorization": "Bearer tk"}
    assert c.post("/api/live/settings", headers=H, json={"mic": "silence"}).json() == {"ok": True}
    assert c.get("/api/live/settings", headers=H).json()["mic"] == "silence"
    assert c.post("/api/live/settings", headers=H, json={"camera": "silence"}).status_code == 400      # «silence» chỉ cho mic


def _fake_nt():
    class NT:
        class MediaSource:
            SHELL, FILE, EXTERNAL = "SHELL", "FILE", "EXT"

        class StreamMode:
            CAPTURE, PLAYBACK = "CAP", "PB"

        @staticmethod
        def AudioDescription(*a):
            return ("AD",) + a

        @staticmethod
        def VideoDescription(*a):
            return ("VD",) + a

        @staticmethod
        def MediaDescription(*a):
            return ("MD",) + a
    return NT


def test_set_capture_swaps_mic_and_camera_during_call():
    import asyncio
    seen = []

    class NTG:
        async def set_stream_sources(self, uid, mode, desc):
            seen.append((mode, desc))

    rt = StubRuntime()
    rt.nt, rt.ntg = _fake_nt(), NTG()
    c = live_call.LiveCall(rt, "+84", StubClient(), 5, False, True, mic="http://r1/mic?k=1", cam="rtsp://cam/s")
    c.state = "active"
    asyncio.run(c.set_capture(mic="silence"))
    mode, md = seen[-1]
    assert mode == "CAP" and md[1][4] == live_call.mic_command("silence") and md[3][0] == "VD" and c.info()["mic_muted"] is True
    asyncio.run(c.set_capture(cam_off=True))
    assert seen[-1][1][3] is None and c.info()["cam_off"] is True            # tắt hình: không còn mô tả video
    asyncio.run(c.set_capture(mic="http://r1/mic?k=1", mic_fmt="s16le16k", cam_off=False))
    md = seen[-1][1]
    assert md[1][4] == live_call.mic_command("http://r1/mic?k=1", "s16le16k") and md[3][0] == "VD" and c.info()["mic_muted"] is False
    n = len(seen)
    c.state = "ringing"                                                          # chưa dựng media: chỉ ghi nhớ, dựng khi nghe máy
    asyncio.run(c.set_capture(mic="silence"))
    assert len(seen) == n and c.mic == "silence"
    assert "k=1" not in str(c.info()), "URL mic (có khoá) không được lộ ra info"


def _start_loop():
    """Vòng lặp asyncio chạy ở luồng riêng (giả vòng lặp NTgCalls); dọn bằng _stop_loop để không để luồng nền sống tới lúc thoát tiến trình."""
    import asyncio
    import threading
    loop = asyncio.new_event_loop()
    th = threading.Thread(target=loop.run_forever, daemon=True)
    th.start()
    return loop, th


def _stop_loop(loop, th, futs=()):
    for f in futs:
        f.result(10)                               # đợi tác vụ nền xong (vd. đọc lời + trả mic) trước khi dừng vòng lặp
    loop.call_soon_threadsafe(loop.stop)
    th.join(10)
    loop.close()


def test_live_media_route_validates_and_applies(tmp_path):
    import asyncio
    cfg = Config(data_dir=str(tmp_path), gateway_token="tk")
    app = build_app(cfg)
    c = TestClient(app, raise_server_exceptions=False)
    H = {"Authorization": "Bearer tk"}
    assert c.post("/api/live/media", json={"id": "x", "mic": "silence"}).status_code == 401
    app.state.live["rt"] = None
    assert c.post("/api/live/media", headers=H, json={"id": "x", "mic": "silence"}).status_code == 404
    loop, th = _start_loop()
    seen = []

    class NTG:
        async def set_stream_sources(self, uid, mode, desc):
            seen.append(desc)

    rt = StubRuntime()
    rt.nt, rt.ntg, rt.loop = _fake_nt(), NTG(), loop
    call = live_call.LiveCall(rt, "+84", StubClient(), 5, False, True)
    call.state = "active"
    rt.calls = {call.id: call}
    app.state.live["rt"] = rt
    try:
        assert c.post("/api/live/media", headers=H, json={"id": call.id}).status_code == 400                       # không có gì để đổi
        assert c.post("/api/live/media", headers=H, json={"id": call.id, "mic": "ftp://x"}).status_code == 400
        assert c.post("/api/live/media", headers=H, json={"id": call.id, "camera": "silence"}).status_code == 400
        r = c.post("/api/live/media", headers=H, json={"id": call.id, "mic": "silence"})
        assert r.status_code == 200 and r.json()["mic_muted"] is True and len(seen) == 1
        r = c.post("/api/live/media", headers=H, json={"id": call.id, "camera_off": True})
        assert r.json()["cam_off"] is True and seen[-1][3] is None
        call.state = "ended"
        assert c.post("/api/live/media", headers=H, json={"id": call.id, "mic": "test"}).status_code == 404
    finally:
        _stop_loop(loop, th)


def test_camera_black_command_and_setting(tmp_path):
    assert "color=c=black" in live_call.camera_command("black") and "testsrc2" not in live_call.camera_command("black")
    c = _client(tmp_path)
    H = {"Authorization": "Bearer tk"}
    assert c.post("/api/live/settings", headers=H, json={"camera": "black"}).json() == {"ok": True}
    assert c.get("/api/live/settings", headers=H).json()["camera"] == "black"


def test_say_swaps_mic_to_speech_then_restores(tmp_path):
    import asyncio
    import os
    import wave
    seen, files = [], []

    class NTG:
        async def set_stream_sources(self, uid, mode, desc):
            seen.append(desc)
            path = desc[1][4].split("-i ")[-1].split(" -vn")[0].strip("'")
            if path.endswith("_say.wav"):
                with wave.open(path) as w:                      # tệp phải là WAV đúng 48 kHz mono 16 bit lúc ffmpeg đọc
                    files.append((w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()))

    rt = StubRuntime()
    rt.data_dir = str(tmp_path)
    rt.nt, rt.ntg = _fake_nt(), NTG()
    c = live_call.LiveCall(rt, "+84", StubClient(), 5, False, False, mic="http://r1/mic?k=1", mic_fmt="s16le16k")
    c.state = "active"
    pcm = b"\x01\x00" * 4800                                   # 0,1 giây
    secs = asyncio.run(c.say(pcm))
    assert abs(secs - 0.1) < 0.01 and len(seen) == 2
    assert files == [(48000, 1, 2, 4800)]
    assert c.mic == "http://r1/mic?k=1" and c.mic_fmt == "s16le16k"            # mic trả về như cũ
    assert not os.path.exists(os.path.join(str(tmp_path), "live", c.id + "_say.wav"))


def test_live_say_route(tmp_path):
    import asyncio
    app = build_app(Config(data_dir=str(tmp_path), gateway_token="tk"))
    c = TestClient(app, raise_server_exceptions=False)
    H = {"Authorization": "Bearer tk"}
    assert c.post("/api/live/say", json={"id": "x", "text": "a"}).status_code == 401
    assert c.post("/api/live/say", headers=H, json={"id": "x", "text": ""}).status_code == 400
    assert c.post("/api/live/say", headers=H, json={"id": "x", "text": "xin chào"}).status_code == 404
    loop, th = _start_loop()
    futs = []

    class NTG:
        async def set_stream_sources(self, uid, mode, desc):
            pass

    class RT(StubRuntime):
        data_dir = str(tmp_path)

        def run(self, coro):
            futs.append(asyncio.run_coroutine_threadsafe(coro, loop))

    rt = RT()
    rt.nt, rt.ntg, rt.loop = _fake_nt(), NTG(), loop
    call = live_call.LiveCall(rt, "+84", StubClient(), 5, False)
    rt.calls = {call.id: call}
    app.state.live["rt"] = rt

    class Synth:
        def synthesize(self, text, voice, rate):
            return b"\x00\x00" * 9600                        # 0,2 giây

    try:
        call.state = "ringing"
        assert c.post("/api/live/say", headers=H, json={"id": call.id, "text": "xin chào"}).status_code == 404   # chưa nghe máy
        call.state = "active"
        app.state.synthesizer = None
        assert c.post("/api/live/say", headers=H, json={"id": call.id, "text": "xin chào"}).status_code == 501
        app.state.synthesizer = Synth()
        r = c.post("/api/live/say", headers=H, json={"id": call.id, "text": "xin chào"})
        assert r.status_code == 200 and r.json() == {"seconds": 0.2}
        assert c.post("/api/live/say", headers=H, json={"id": call.id, "audio_b64": "!!"}).status_code == 400          # không phải base64
        from gateway import chat_routes
        monkey = chat_routes.to_pcm48k
        chat_routes.to_pcm48k = lambda raw: b"\x00\x00" * 4800                                                      # ffmpeg giả: 0,1 giây
        try:
            import base64
            r = c.post("/api/live/say", headers=H, json={"id": call.id, "audio_b64": base64.b64encode(b"mp3").decode()})
            assert r.status_code == 200 and r.json() == {"seconds": 0.1}
        finally:
            chat_routes.to_pcm48k = monkey
    finally:
        _stop_loop(loop, th, futs)


def test_monitor_reports_ratio_and_flags_a_starved_source(monkeypatch):
    import asyncio
    monkeypatch.setattr(live_call, "MONITOR_EVERY", 0.05)
    caps = iter([0.0, 0.05, 0.10, 0.101, 0.102, 0.103])       # 2 lần đầu chạy đúng nhịp, rồi nguồn đứng

    class NTG:
        async def time(self, uid, mode):
            return next(caps, 0.103)

    rt = StubRuntime()
    rt.nt, rt.ntg = _fake_nt(), NTG()
    c = live_call.LiveCall(rt, "+84", StubClient(), 5, False)
    c.state = "active"

    async def go():
        t = asyncio.ensure_future(c._monitor())
        await asyncio.sleep(0.45)
        c.state = "ended"
        await asyncio.sleep(0.1)
        t.cancel()
    asyncio.run(go())
    assert c.diag.get("ratio") is not None and "capture_s" in c.diag
    assert any("nguồn gửi đi chậm" in e["e"] for e in c.events)
    assert "diag" in c.info()


def test_peer_video_flags_from_remote_source_and_media_state_and_video_can_be_enabled_mid_call():
    import asyncio
    seen = []

    class NTG:
        async def set_stream_sources(self, uid, mode, desc):
            seen.append(desc)

    class RT(StubRuntime):
        calls = {}
        events = []
        _first_frame_logged = False

        def by_user(self, uid):
            return call if uid == 5 else None

        def on_event(self, kind, c):
            self.events.append(kind)
    rt = RT()
    rt.nt, rt.ntg = _fake_nt(), NTG()
    call = live_call.LiveCall(rt, "+84", StubClient(), 5, False, False)
    call.state = "active"
    rt.calls = {call.id: call}
    R = live_call.CallRuntime
    rt._set_peer_video = lambda c, on, why: R._set_peer_video(rt, c, on, why)

    class Dev:
        name = "CAMERA"

    class St:
        name = "ACTIVE"

    class Src:
        device, state = Dev(), St()
    R._remote_source(rt, 5, Src())
    assert call.peer_video is True and rt.events == ["peer_video"] and call.info()["peer_video"] is True
    R._remote_source(rt, 5, Src())                                   # lặp lại: không bắn sự kiện thêm
    assert rt.events == ["peer_video"]

    class Media:
        muted, video_stopped, video_paused = False, True, False
    R._upgrade(rt, 5, Media())
    assert call.peer_video is False and rt.events == ["peer_video", "peer_video"]
    # bật video giữa cuộc gọi thoại: mô tả gửi đi có camera từ đó
    assert call.video is False
    asyncio.run(call.set_capture(cam="http://cam/s", video=True))
    assert call.video is True and call.cam_off is False and seen[-1][3][0] == "VD"


def test_video_view_encodes_i420_frames_to_jpeg_with_real_ffmpeg():
    import shutil
    import time
    if not shutil.which("ffmpeg"):
        pytest.skip("không có ffmpeg")
    v = live_call.VideoView()
    w, h = 64, 36
    frame = bytes([128]) * (w * h) + bytes([128]) * (w * h // 2)                   # I420 xám
    v.push(b"x", w, h)                                                              # sai độ dài: bỏ, đếm lỗi
    assert v.bad == 1 and v.frames == 0
    for _ in range(12):
        v.push(frame, w, h)
        time.sleep(0.05)
    end = time.time() + 6
    while not v.seq and time.time() < end:
        time.sleep(0.1)
    try:
        assert v.seq >= 1 and v.jpeg[:2] == b"\xff\xd8" and v.jpeg[-2:] == b"\xff\xd9", v.info()
        assert v.info()["size"] == "64x36"
        v.push(bytes([128]) * (32 * 18 * 3 // 2), 32, 18)                           # đổi kích thước: dựng lại ffmpeg
        assert v.info()["size"] == "32x18"
    finally:
        v.close()


def test_frames_from_camera_device_go_to_the_view_not_the_audio_counters():
    pushed = []

    class FakeView:
        def push(self, data, w, h):
            pushed.append((len(data), w, h))

    class RT(StubRuntime):
        _first_frame_logged = False

        def by_user(self, uid):
            return call
    rt = RT()
    call = live_call.LiveCall(rt, "+84", StubClient(), 5, False, True)
    call.view = FakeView()

    class Dev:
        name = "CAMERA"

    class FD:
        width, height = 8, 4

    class Fr:
        data, frame_data = b"\x00" * 48, FD()
    live_call.CallRuntime._frames(rt, 5, None, Dev(), [Fr()])
    assert pushed == [(48, 8, 4)] and call.frames_in == 0


def test_live_view_routes_stream_mjpeg_and_snapshot_and_return_409_without_video(tmp_path):
    app = build_app(Config(data_dir=str(tmp_path), gateway_token="tk"))
    c = TestClient(app, raise_server_exceptions=False)
    H = {"Authorization": "Bearer tk"}
    assert c.get("/api/live/view/x").status_code == 401
    assert c.get("/api/live/view/x", headers=H).status_code == 404
    rt = StubRuntime()
    call = live_call.LiveCall(rt, "+84", StubClient(), 5, False, True)
    call.state = "active"
    rt.calls = {call.id: call}
    app.state.live["rt"] = rt
    assert c.get("/api/live/snapshot/%s" % call.id, headers=H).status_code == 409
    call.view.jpeg, call.view.seq = b"\xff\xd8JPEGDATA\xff\xd9", 1
    r = c.get("/api/live/snapshot/%s" % call.id, headers=H)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == call.view.jpeg
    with c.stream("GET", "/api/live/view/%s" % call.id, headers=H) as s:
        assert s.status_code == 200 and "multipart/x-mixed-replace" in s.headers["content-type"]
        first = next(s.iter_bytes())
        assert b"--frame" in first and b"image/jpeg" in first and b"JPEGDATA" in first
        call.state = "ended"                                                         # cuộc gọi hết: luồng tự dừng


def test_p2p_tat_mac_dinh_nhu_zalo(monkeypatch):
    """Docker bridge công bố IP container → P2P hỏng; mặc định tắt cả khai báo lẫn connect_p2p, TELEGRAM_P2P=1 bật lại."""
    monkeypatch.delenv("TELEGRAM_P2P", raising=False)
    assert live_call.p2p_enabled() is False
    for v in ("1", "true", "ON"):
        monkeypatch.setenv("TELEGRAM_P2P", v)
        assert live_call.p2p_enabled() is True
    monkeypatch.setenv("TELEGRAM_P2P", "0")
    assert live_call.p2p_enabled() is False


def test_protocol_khai_bao_p2p_theo_cong_tat_p2p(monkeypatch):
    import types

    class Nt:
        class NTgCalls:
            @staticmethod
            def get_protocol():
                return types.SimpleNamespace(udp_p2p=True, udp_reflector=True, min_layer=92, max_layer=92, library_versions=["11.0.0"])

    rt = live_call.CallRuntime.__new__(live_call.CallRuntime)
    rt.nt = Nt
    monkeypatch.delenv("TELEGRAM_P2P", raising=False)
    assert rt.protocol()["udp_p2p"] is False and rt.protocol()["udp_reflector"] is True
    monkeypatch.setenv("TELEGRAM_P2P", "1")
    assert rt.protocol()["udp_p2p"] is True


def test_outgoing_video_call_reapplies_capture_after_connected_but_other_calls_do_not():
    import asyncio
    import types
    sets = []

    class Ntg:
        async def set_stream_sources(self, uid, mode, desc):
            sets.append((uid, mode, desc))

    rt = types.SimpleNamespace(ntg=Ntg(), data_dir="/tmp", nt=types.SimpleNamespace(StreamMode=types.SimpleNamespace(CAPTURE="CAP")))
    c = live_call.LiveCall(rt, "+84", None, 5, True, True)
    c._capture_description = lambda: "mô tả"
    c.state = "connecting"
    asyncio.run(c.refresh_capture((0.01, 0.01)))
    assert sets == [], "chưa nối xong (state connecting) thì không bật lại"
    c.state = "active"
    asyncio.run(c.refresh_capture((0.01, 0.01)))
    assert sets == [(5, "CAP", "mô tả")] * 2 and any("bật lại nguồn gửi đi" in e["e"] for e in c.events)
    c.state = "active"; sets.clear()

    async def stop_midway():
        t = asyncio.ensure_future(c.refresh_capture((0.01, 0.2)))
        await asyncio.sleep(0.05)
        c.state = "ended"
        await t
    asyncio.run(stop_midway())
    assert len(sets) == 1, "cuộc gọi kết thúc giữa chừng thì dừng, không đặt nguồn lần hai"


def test_connected_schedules_the_capture_refresh_only_for_outgoing_video_calls():
    import types
    ran = []
    rt = live_call.CallRuntime.__new__(live_call.CallRuntime)
    rt.calls, rt.on_event, rt.data_dir = {}, (lambda *a: None), "/tmp"
    rt.run = lambda coro: (ran.append(coro), coro.close())
    connected = types.SimpleNamespace(state=types.SimpleNamespace(name="CONNECTED"))
    for uid, outgoing, video, expect in ((11, True, True, 1), (12, True, False, 0), (13, False, True, 0)):
        ran.clear()
        c = live_call.LiveCall(rt, "+84", None, uid, outgoing, video)
        rt.calls[c.id] = c
        rt._conn(uid, connected)
        assert len(ran) == expect, (uid, outgoing, video, len(ran))


def test_hangup_tells_telegram_the_duration_and_that_it_was_a_video_call():
    """discardCall (td_api.tl): duration + is_video để lịch sử trò chuyện ghi đúng «cuộc gọi video» và thời lượng; cuộc chưa nối thì duration 0."""
    rt, cl = StubRuntime(), StubClient()
    c = live_call.LiveCall(rt, "+84", cl, 5, True, True)
    c.tdlib_call_id = 7
    c.active_at = time.time() - 42.4
    c.hangup()
    assert cl.discarded == [7] and cl.discard_args["is_video"] is True and 41 <= cl.discard_args["duration"] <= 43, cl.discard_args
    d = live_call.LiveCall(rt, "+84", cl, 6, True, False)
    d.tdlib_call_id = 8
    d.hangup()
    assert cl.discard_args == {"duration": 0, "is_video": False}, "chưa nối máy: duration 0, cuộc thoại"
