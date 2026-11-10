"""Cầu nối thiết bị của telegram_call — chạy NGOÀI Home Assistant.

HA không có sẵn ở môi trường test nên các module `homeassistant.*` được thay bằng bản giả tối thiểu; test chạy THẬT bridge_core.py,
devices.py, api.py và bridge.py (phân giải mic/camera/loa, mặc định, ghi đè mỗi cuộc gọi, âm lượng loa, đẩy mặc định sang gateway).
Không chứng minh chạy được trong HA thật (việc đó kiểm bằng nạp module dưới HA 2026.11) — chỉ chứng minh logic điều phối đúng.
"""
import asyncio
import importlib.util
import os
import sys
import types

ROOT = os.path.join(os.path.dirname(__file__), "..", "custom_components", "telegram_call")


def _load():
    def mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class HomeAssistantError(Exception):
        pass

    class NoURLAvailableError(Exception):
        pass

    holder = {"urls": {}, "saved": {}, "registry": {}, "entries": {}, "base": "http://ha:8123"}

    class Store:
        def __init__(self, hass, version, key):
            self.key = key

        async def async_load(self):
            return holder["saved"].get(self.key)

        async def async_save(self, data):
            holder["saved"][self.key] = dict(data)

    mod("homeassistant")
    mod("homeassistant.components")
    cam = mod("homeassistant.components.camera")
    mod("homeassistant.core", HomeAssistant=object)
    mod("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
    helpers = mod("homeassistant.helpers")
    reg_mod = mod("homeassistant.helpers.entity_registry")
    mod("homeassistant.helpers.storage", Store=Store)

    def get_url(hass, **kw):
        if not holder["base"]:
            raise NoURLAvailableError()
        return holder["base"]
    mod("homeassistant.helpers.network", NoURLAvailableError=NoURLAvailableError, get_url=get_url)
    sys.modules["homeassistant.components"].camera = cam
    helpers.entity_registry = reg_mod
    reg_mod.async_get = lambda hass: types.SimpleNamespace(async_get=lambda eid: holder["registry"].get(eid))

    async def stream_source(hass, entity_id):
        return holder["urls"].get(entity_id)
    cam.async_get_stream_source = stream_source

    pkg = types.ModuleType("telegram_call")
    pkg.__path__ = [ROOT]
    sys.modules["telegram_call"] = pkg
    for name in ("const", "api", "bridge_core", "devices", "bridge"):
        spec = importlib.util.spec_from_file_location("telegram_call." + name, os.path.join(ROOT, name + ".py"))
        m = importlib.util.module_from_spec(spec)
        sys.modules["telegram_call." + name] = m
        spec.loader.exec_module(m)
    for n in ("bridge", "bridge_core", "const", "api", "devices"):
        holder[n] = sys.modules["telegram_call." + n]
    holder["HAError"] = HomeAssistantError
    return holder


H = _load()
core, bridge_mod, devices_mod, api_mod = H["bridge_core"], H["bridge"], H["devices"], H["api"]


# ── Thuần: trạng thái, nhãn, địa chỉ ───────────────────────────────────────────────────────────────────

def test_derive_state_covers_gateway_states():
    d = core.derive_state
    assert d({"state": "dialing", "outgoing": True}, False) == "ringing"
    assert d({"state": "ringing", "outgoing": True}, False) == "ringing"
    assert d({"state": "ringing", "outgoing": False}, False) == "incoming"
    assert d({"state": "connecting"}, False) == "connecting"
    assert d({"state": "active"}, False) == "answered"
    assert d({"state": "ended"}, True) == "ended"
    assert d({"state": "ended", "outgoing": True}, False) == "not_answered"        # gateway không phân biệt từ chối / không nghe
    assert d({"state": "ended", "outgoing": False}, False) == "missed"
    assert d({"state": "ended", "error": "boom"}, False) == "error"
    assert d({"state": "la-hoac"}, False) is None


def test_intercom_url_and_pick_devices():
    assert core.intercom_url("http://ha:8123/", "r1_speaker", "E1", "k e/y") == "http://ha:8123/api/r1_speaker/intercom/E1?k=k%20e%2Fy"
    assert core.intercom_url("http://ha", "dahua_talk", "D9", "tok", "duplex") == "http://ha/api/dahua_talk/duplex/D9?k=tok"
    got = core.pick_devices({"speaker": "media_player.loa", "mic": "camera.c1", "mic_url": "", "camera": "camera.c1"},
                            {"speaker": "media_player.khac", "mic": ""})
    assert got["speaker"] == "media_player.khac" and got["mic"] == "" and got["camera"] == "camera.c1"


def test_defaults_clamp_call_volume_and_store_speaker():
    dd = devices_mod.DeviceDefaults(types.SimpleNamespace(), "e1")
    assert dd.get("call_volume") == 0 and dd.get("speaker") == ""
    dd._merge({"call_volume": 250, "speaker": " Loa · media_player.r1 "})
    assert dd.get("call_volume") == 100 and dd.get("speaker") == "Loa · media_player.r1"
    dd._merge({"call_volume": -5})
    assert dd.get("call_volume") == 0
    dd._merge({"call_volume": "60"})
    assert dd.get("call_volume") == 60
    dd._merge({"call_volume": "abc"})                      # sai kiểu: giữ nguyên
    assert dd.get("call_volume") == 60


# ── Cầu nối ────────────────────────────────────────────────────────────────────────────────────────────

class FakeApi:
    def __init__(self):
        self.pushed = []
        self.fail = False

    async def set_live_settings(self, patch):
        if self.fail:
            raise api_mod.TelegramCallError("gateway tắt")
        self.pushed.append(dict(patch))


class FakeServices:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False):
        self.calls.append((domain, service, dict(data)))


class FakeStates:
    def __init__(self):
        self.volume = {}

    def get(self, entity_id):
        if entity_id not in self.volume:
            return None
        return types.SimpleNamespace(attributes={"volume_level": self.volume[entity_id]})


class FakeConfigEntries:
    def async_get_entry(self, entry_id):
        return H["entries"].get(entry_id)


class FakeHass:
    def __init__(self):
        self.services, self.states, self.config_entries = FakeServices(), FakeStates(), FakeConfigEntries()


def _make(defaults, base="http://ha:8123"):
    H["urls"] = {"camera.cam1": "rtsp://u:p@cam.local/stream", "camera.cam2": "rtsp://cam2.local/s"}
    H["registry"], H["entries"], H["saved"], H["base"] = {}, {}, {}, base
    hass, api = FakeHass(), FakeApi()
    dd = devices_mod.DeviceDefaults(hass, "e1")
    dd._merge(defaults)
    return hass, api, dd, bridge_mod.CallBridge(hass, api, dd)


def _ent(domain, entity_id, config_entry_id, **data):
    H["registry"][entity_id] = types.SimpleNamespace(config_entry_id=config_entry_id, device_id=None)
    H["entries"][config_entry_id] = types.SimpleNamespace(domain=domain, entry_id=config_entry_id, options={}, data=dict(data))


def test_speaker_sink_only_for_r1_and_dahua_intercom():
    async def run():
        hass, api, dd, br = _make({})
        _ent("r1_speaker", "media_player.r1_loa", "r1entry", intercom_key="KHOA")
        _ent("dahua_talk", "media_player.cam_loa", "dhentry", intercom_key="K2")
        _ent("sonos", "media_player.sonos", "sonosentry")
        _ent("r1_speaker", "media_player.khong_khoa", "nokey")
        assert await br.async_speaker_sink("media_player.r1_loa") == "http://ha:8123/api/r1_speaker/intercom/r1entry?k=KHOA"
        assert await br.async_speaker_sink("media_player.cam_loa") == "http://ha:8123/api/dahua_talk/intercom/dhentry?k=K2"
        assert await br.async_speaker_sink("media_player.sonos") == ""          # không có cửa bộ đàm
        assert await br.async_speaker_sink("media_player.khong_khoa") == ""     # chưa có khoá
        assert await br.async_speaker_sink("media_player.khong_co") == "" and await br.async_speaker_sink("") == ""
        br.defaults._merge({"ha_url": ""})
        H["base"] = ""
        assert await br.async_speaker_sink("media_player.r1_loa") == ""         # HA chưa có địa chỉ nội bộ
    asyncio.run(run())


def test_call_fields_carry_mic_camera_and_speaker_with_override():
    async def run():
        hass, api, dd, br = _make({"mic": "camera.cam1", "camera": "camera.cam2", "speaker": "Loa · media_player.r1_loa"})
        _ent("r1_speaker", "media_player.r1_loa", "r1entry", intercom_key="KHOA")
        f = await br.async_call_fields(None)
        assert f == {"mic": "rtsp://u:p@cam.local/stream", "mic_format": "auto", "camera": "rtsp://cam2.local/s",
                     "speaker": "http://ha:8123/api/r1_speaker/intercom/r1entry?k=KHOA"}, f
        f = await br.async_call_fields({"camera": "camera.cam1", "speaker": ""})       # ghi đè: camera khác, tắt loa
        assert f["camera"] == "rtsp://u:p@cam.local/stream" and "speaker" not in f
    asyncio.run(run())


def test_push_defaults_send_speaker_and_test_only_when_nothing_selected():
    async def run():
        hass, api, dd, br = _make({"speaker": "media_player.r1_loa"})
        _ent("r1_speaker", "media_player.r1_loa", "r1entry", intercom_key="KHOA")
        await br.async_push_defaults()
        assert api.pushed[-1] == {"mic": "test", "mic_format": "auto", "camera": "test",
                                  "speaker": "http://ha:8123/api/r1_speaker/intercom/r1entry?k=KHOA"}
        assert br.policy.pushed_ok is True
        # Loa không hỗ trợ -> gửi rỗng (gateway lưu file thay vì đẩy), không coi là thiếu.
        _ent("sonos", "media_player.sonos", "sn")
        await dd.async_set(speaker="media_player.sonos")
        await br.async_push_defaults()
        assert api.pushed[-1]["speaker"] == "" and br.policy.pushed_ok is True
    asyncio.run(run())


def test_push_camera_not_ready_keeps_gateway_value_and_retries_then_gives_up():
    """Cùng lớp lỗi đã gặp ở zalo_call: thiết bị ĐÃ CHỌN mà chưa phân giải được thì không gửi «test» ghi đè giá trị đang đúng."""
    async def run():
        hass, api, dd, br = _make({"camera": "camera.cam2", "mic": "camera.cam1"})
        H["urls"].pop("camera.cam2")                         # camera chưa nạp xong lúc HA khởi động
        H["urls"].pop("camera.cam1")
        await br.async_push_defaults()
        assert "camera" not in api.pushed[-1] and "mic" not in api.pushed[-1] and "mic_format" not in api.pushed[-1], api.pushed[-1]
        assert br.policy.pushed_ok is False                  # chưa xong: coordinator thử lại
        H["urls"]["camera.cam2"] = "rtsp://cam2.local/s"
        H["urls"]["camera.cam1"] = "rtsp://u:p@cam.local/stream"
        await br.async_push_defaults()
        assert api.pushed[-1]["camera"] == "rtsp://cam2.local/s" and api.pushed[-1]["mic"] == "rtsp://u:p@cam.local/stream"
        assert br.policy.pushed_ok is True
        # Người dùng THẬT SỰ bỏ chọn camera -> «test».
        await dd.async_set(camera="")
        await br.async_push_defaults()
        assert api.pushed[-1]["camera"] == "test" and br.policy.pushed_ok is True
        # Camera bị xoá hẳn: thử tối đa N lần rồi thôi, không lặp mãi.
        await dd.async_set(camera="camera.cam2")
        H["urls"].pop("camera.cam2")
        for _ in range(bridge_mod._PUSH_THIEU_TOI_DA - 1):
            await br.async_push_defaults()
            assert br.policy.pushed_ok is False
        await br.async_push_defaults()
        assert br.policy.pushed_ok is True and "camera" not in api.pushed[-1]
    asyncio.run(run())


def test_push_gateway_down_marks_not_pushed_and_hides_secrets():
    async def run():
        hass, api, dd, br = _make({})
        api.fail = True
        await br.async_push_defaults()
        assert br.policy.pushed_ok is False and api.pushed == []
    asyncio.run(run())


# ── Âm lượng loa theo cuộc gọi ─────────────────────────────────────────────────────────────────────────

def test_call_volume_set_on_answer_and_restored_at_end():
    async def run():
        hass, api, dd, br = _make({"speaker": "media_player.loa1", "call_volume": 77})
        hass.states.volume["media_player.loa1"] = 11 / 15
        await br.start("c1")
        assert hass.services.calls == [("media_player", "volume_set", {"entity_id": "media_player.loa1", "volume_level": 0.77})]
        hass.states.volume["media_player.loa1"] = 12 / 15                  # R1 làm tròn lên nấc 12/15
        await br.start("c1")                                               # nghe máy lặp: không đặt lại
        assert len(hass.services.calls) == 1
        await br.stop("c1")
        assert abs(hass.services.calls[-1][2]["volume_level"] - 11 / 15) < 1e-9
        n = len(hass.services.calls)
        await br.stop("c1")                                                # dừng lặp: vô hại
        assert len(hass.services.calls) == n
    asyncio.run(run())


def test_call_volume_not_restored_when_user_changed_it_and_zero_leaves_speaker_alone():
    async def run():
        hass, api, dd, br = _make({"speaker": "media_player.loa1", "call_volume": 60})
        hass.states.volume["media_player.loa1"] = 0.3
        await br.start("a")
        hass.states.volume["media_player.loa1"] = 1.0                      # người dùng tự kéo hết cỡ
        n = len(hass.services.calls)
        await br.stop("a")
        assert len(hass.services.calls) == n
        # Mặc định 60 nhưng cuộc gọi này đặt volume=0 -> không đụng vào loa; volume=40 thắng mặc định 0.
        hass, api, dd, br = _make({"speaker": "media_player.loa1", "call_volume": 60})
        hass.states.volume["media_player.loa1"] = 0.5
        br.remember(None, {"volume": 0})
        await br.start("b")
        assert hass.services.calls == []
        hass, api, dd, br = _make({"speaker": "media_player.loa1"})
        hass.states.volume["media_player.loa1"] = 0.5
        br.remember(None, {"volume": 40})
        await br.start("c")
        assert hass.services.calls[0][2]["volume_level"] == 0.4
        assert br.volume_for({"volume": "x"}) == 0 and br.volume_for({"volume": 999}) == 100
    asyncio.run(run())


def test_call_override_speaker_gets_the_volume_and_shutdown_restores():
    async def run():
        hass, api, dd, br = _make({"speaker": "media_player.loa1", "call_volume": 50})
        hass.states.volume["media_player.khac"] = 0.2
        br.remember(None, {"speaker": "media_player.khac"})                # cuộc gọi này dùng loa khác
        await br.start("x")
        assert hass.services.calls[0][2] == {"entity_id": "media_player.khac", "volume_level": 0.5}
        hass.states.volume["media_player.khac"] = 0.5
        await br.async_shutdown()                                          # tải lại tích hợp giữa cuộc gọi: trả mức cũ
        assert hass.services.calls[-1][2] == {"entity_id": "media_player.khac", "volume_level": 0.2}
        # Không có loa -> không làm gì.
        hass, api, dd, br = _make({"call_volume": 50})
        await br.start("y")
        assert hass.services.calls == []
    asyncio.run(run())


# ── API gateway ────────────────────────────────────────────────────────────────────────────────────────

class _Resp:
    status = 200

    def __init__(self, body):
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self):
        self.requests = []

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        return _Resp({"id": "c1", "state": "dialing"})


def test_api_live_call_and_settings_bodies():
    async def run():
        s = _Session()
        api = api_mod.TelegramGatewayApi(s, "http://gw:3333/", "TOKEN", "+84")
        await api.live_call(5, True, mic="rtsp://m", mic_format="auto", camera=None, speaker="http://ha/api/r1_speaker/intercom/E?k=1")
        m, url, kw = s.requests[-1]
        assert (m, url) == ("POST", "http://gw:3333/api/live/call")
        assert kw["json"] == {"user_id": 5, "video": True, "mic": "rtsp://m", "mic_format": "auto",
                              "speaker": "http://ha/api/r1_speaker/intercom/E?k=1"}          # trường rỗng không gửi
        assert kw["headers"]["Authorization"] == "Bearer TOKEN" and kw["params"] == {"account": "+84"}
        await api.set_live_settings({"camera": "test"})
        assert s.requests[-1][2]["json"] == {"camera": "test"}                              # chỉ trường có mặt
    asyncio.run(run())


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
    print("ALL TELEGRAM BRIDGE TESTS PASSED")
