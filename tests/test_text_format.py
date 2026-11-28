"""Kiểm thử định dạng chữ, tự nhấn mạnh, thực thể cài đặt và dịch vụ nhắn tin của telegram_call."""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
import types

import pytest
import voluptuous as vol

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "custom_components", "telegram_call"))


def _load_isolated() -> dict:
    """Nạp các stub homeassistant.* và module telegram_call.* rồi khôi phục sys.modules ngay sau đó."""
    snapshot = dict(sys.modules)
    holder: dict = {"saved": {}, "session": None}

    def mod(name: str, **attrs) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class HomeAssistantError(Exception):
        pass

    class ServiceValidationError(HomeAssistantError):
        pass

    class NoURLAvailableError(Exception):
        pass

    class Store:
        def __init__(self, hass, version, key):
            self.key = key

        async def async_load(self):
            val = holder["saved"].get(self.key)
            return dict(val) if isinstance(val, dict) else val

        async def async_save(self, data):
            holder["saved"][self.key] = dict(data)

    class Entity:
        def async_on_remove(self, func):
            self._unsubs = getattr(self, "_unsubs", [])
            self._unsubs.append(func)

        def async_write_ha_state(self):
            pass

        async def async_added_to_hass(self):
            pass

    class RestoreEntity(Entity):
        async def async_added_to_hass(self):
            await super().async_added_to_hass()

        async def async_get_last_state(self):
            return getattr(self, "_fake_last_state", None)

    class SelectEntity(Entity):
        _attr_options: list[str] = []

        @property
        def options(self) -> list[str]:
            return list(self._attr_options)

    class SwitchEntity(Entity):
        pass

    class EntityCategory:
        CONFIG = "config"

    class SupportsResponse:
        NONE = "none"
        OPTIONAL = "optional"
        ONLY = "only"

    def _cv_boolean(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes", "on", "enable"):
                return True
            if v in ("false", "0", "no", "off", "disable"):
                return False
            raise vol.Invalid(f"invalid boolean value {value}")
        if isinstance(value, (int, float)):
            return bool(value)
        raise vol.Invalid(f"invalid boolean value {value}")

    def _cv_string(value):
        if value is None or isinstance(value, (dict, list, tuple, set)):
            raise vol.Invalid("invalid string value")
        return str(value)

    def _cv_ensure_list(value):
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, tuple):
            return list(value)
        return [value]

    def _cv_has_at_least_one_key(*keys):
        key_set = set(keys)

        def _validate(obj):
            if not isinstance(obj, dict) or not (key_set & set(obj.keys())):
                raise vol.Invalid(f"must contain at least one of {', '.join(keys)}")
            return obj

        return _validate

    try:
        mod("homeassistant")
        comps = mod("homeassistant.components")
        cam = mod("homeassistant.components.camera", async_get_stream_source=lambda hass, eid: None)
        sel_mod = mod("homeassistant.components.select", SelectEntity=SelectEntity)
        sw_mod = mod("homeassistant.components.switch", SwitchEntity=SwitchEntity)
        comps.camera, comps.select, comps.switch = cam, sel_mod, sw_mod

        mod("homeassistant.config_entries", ConfigEntry=object)
        mod("homeassistant.const", EntityCategory=EntityCategory)
        mod("homeassistant.core", HomeAssistant=object, ServiceCall=object, ServiceResponse=object,
            SupportsResponse=SupportsResponse, callback=lambda fn: fn)
        mod("homeassistant.exceptions", HomeAssistantError=HomeAssistantError,
            ServiceValidationError=ServiceValidationError)

        cv = mod("homeassistant.helpers.config_validation", boolean=_cv_boolean, string=_cv_string,
                 ensure_list=_cv_ensure_list, has_at_least_one_key=_cv_has_at_least_one_key,
                 entity_id=_cv_string)
        helpers = mod("homeassistant.helpers", config_validation=cv)
        reg_mod = mod("homeassistant.helpers.entity_registry", async_get=lambda hass: None)
        dev_mod = mod("homeassistant.helpers.device_registry",
                      DeviceInfo=lambda **kw: dict(kw), async_get=lambda hass: None)
        mod("homeassistant.helpers.storage", Store=Store)
        mod("homeassistant.helpers.network", NoURLAvailableError=NoURLAvailableError,
            get_url=lambda hass, **kw: "http://ha:8123")
        mod("homeassistant.helpers.entity", Entity=Entity)
        mod("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
        mod("homeassistant.helpers.event", async_track_time_interval=lambda hass, cb, iv: (lambda: None))
        mod("homeassistant.helpers.restore_state", RestoreEntity=RestoreEntity)
        mod("homeassistant.helpers.aiohttp_client", async_get_clientsession=lambda hass: holder["session"])
        helpers.entity_registry = reg_mod
        helpers.device_registry = dev_mod

        pkg = types.ModuleType("telegram_call")
        pkg.__path__ = [ROOT]
        sys.modules["telegram_call"] = pkg

        # Stub tối thiểu cho call_rules (services.py import ở đầu tệp; test nhắn tin không gọi thoại).
        call_rules_stub = types.ModuleType("telegram_call.call_rules")
        call_rules_stub.CallVoice = type("CallVoice", (), {})
        sys.modules["telegram_call.call_rules"] = call_rules_stub
        pkg.call_rules = call_rules_stub

        for name in ("const", "echo_policy", "rules", "bridge_core", "api", "devices",
                     "bridge", "entity", "select", "switch", "catalog", "services"):
            spec = importlib.util.spec_from_file_location(
                f"telegram_call.{name}", os.path.join(ROOT, f"{name}.py")
            )
            m = importlib.util.module_from_spec(spec)
            sys.modules[f"telegram_call.{name}"] = m
            setattr(pkg, name, m)
            spec.loader.exec_module(m)
            holder[name] = m

        holder["HomeAssistantError"] = HomeAssistantError
        holder["ServiceValidationError"] = ServiceValidationError
    finally:
        sys.modules.clear()
        sys.modules.update(snapshot)

    return holder


M = _load_isolated()
api_mod = M["api"]
const_mod = M["const"]
devices_mod = M["devices"]
select_mod = M["select"]
switch_mod = M["switch"]
services_mod = M["services"]


# ── Giả lập HTTP session & Home Assistant ──────────────────────────────────────────────────────────────

class _Cookie:
    def __init__(self, value: str):
        self.value = value


class _Resp:
    def __init__(self, body, status: int = 200, cookie: str | None = None):
        self._body = body
        self.status = status
        self.cookies = {"tggw_session": _Cookie(cookie)} if cookie else {}

    class _H:
        def getall(self, name, default=None):
            return default or []

    headers = _H()

    async def json(self, content_type=None):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.logins = 0

    def post(self, url, **kw):
        self.requests.append(("POST", url, kw))
        self.logins += 1
        return _Resp({"ok": True}, 200, f"SESS{self.logins}")

    def request(self, method, url, **kw):
        self.requests.append((method, url, kw))
        return _Resp({
            "id": 101,
            "chat_id": 1,
            "kind": "text",
            "text": "ok",
            "messages": [{"id": 201, "chat_id": 1, "kind": "photo"}, {"id": 202, "chat_id": 1, "kind": "photo"}],
        })


class _FakeServices:
    def __init__(self):
        self.registered: dict[tuple[str, str], tuple] = {}

    def async_register(self, domain, service, handler, schema=None, supports_response=None):
        self.registered[(domain, service)] = (handler, schema, supports_response)

    async def async_call(self, domain, service, service_data, blocking=False, return_response=False):
        handler, schema, _ = self.registered[(domain, service)]
        data = schema(dict(service_data)) if schema is not None else dict(service_data)
        return await handler(types.SimpleNamespace(domain=domain, service=service, data=data))


class _FakeStates:
    def get(self, entity_id):
        return None


class _FakeConfigEntries:
    def __init__(self, entries):
        self._entries = list(entries)

    def async_loaded_entries(self, domain):
        return list(self._entries)


class _FakeHass:
    def __init__(self, entry):
        self.services = _FakeServices()
        self.states = _FakeStates()
        self.config_entries = _FakeConfigEntries([entry])
        self.config = types.SimpleNamespace(is_allowed_path=lambda p: True)

    async def async_add_executor_job(self, target, *args):
        return target(*args)


def _setup_services():
    M["saved"] = {}
    session = _Session()
    M["session"] = session
    api = api_mod.TelegramGatewayApi(session, "http://gw:3333", "u", "p", "+84")
    hass_stub = types.SimpleNamespace()
    defaults = devices_mod.DeviceDefaults(hass_stub, "e1")
    rt = types.SimpleNamespace(api=api, defaults=defaults)
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=rt, data={})
    hass = _FakeHass(entry)
    services_mod.async_register_services(hass)
    return hass, defaults, session


# ── 1. api.py ──────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_api_format_and_auto_emphasis():
    s = _Session()
    api = api_mod.TelegramGatewayApi(s, "http://gw:3333", "u", "p", "+84")

    await api.send_text(123, "Xin chào", fmt="markdown", auto_emphasis=True)
    body = s.requests[-1][2]["json"]
    assert body["format"] == "markdown"
    assert body["auto_emphasis"] is True
    assert "parse_mode" not in body

    await api.send_album(123, [("photo", "a.jpg", b"1", "c1"), ("photo", "b.jpg", b"2", "c2")],
                         fmt="markdown", auto_emphasis=True)
    body = s.requests[-1][2]["json"]
    assert body["format"] == "markdown"
    assert body["auto_emphasis"] is True
    assert "parse_mode" not in body

    await api.edit_message(123, 55, "Sửa", fmt="markdown", auto_emphasis=True)
    body = s.requests[-1][2]["json"]
    assert body["format"] == "markdown"
    assert body["auto_emphasis"] is True
    assert "parse_mode" not in body


@pytest.mark.asyncio
async def test_api_send_file_auto_emphasis_query_is_str():
    s = _Session()
    api = api_mod.TelegramGatewayApi(s, "http://gw:3333", "u", "p", "+84")

    await api.send_file(123, "photo", "a.jpg", b"abc", caption="hi", fmt="markdown", auto_emphasis=True)
    params = s.requests[-1][2]["params"]
    assert params["format"] == "markdown"
    assert type(params["auto_emphasis"]) is str
    assert params["auto_emphasis"] == "true"
    assert "parse_mode" not in params

    await api.send_file(123, "photo", "a.jpg", b"abc", caption="hi", fmt="plain", auto_emphasis=False)
    params = s.requests[-1][2]["params"]
    assert params["format"] == "plain"
    assert type(params["auto_emphasis"]) is str
    assert params["auto_emphasis"] == "false"


@pytest.mark.asyncio
async def test_api_ai_markup_in_body_and_query():
    s = _Session()
    api = api_mod.TelegramGatewayApi(s, "http://gw:3333", "u", "p", "+84")

    await api.send_text(123, "abc", fmt="markdown", auto_emphasis=True, ai_markup="bold:1-2")
    assert s.requests[-1][2]["json"]["ai_markup"] == "bold:1-2"

    await api.send_album(123, [("photo", "a.jpg", b"1", "c"), ("photo", "b.jpg", b"2", "")],
                         fmt="markdown", ai_markup="bold:1-2")
    assert s.requests[-1][2]["json"]["ai_markup"] == "bold:1-2"

    await api.edit_message(123, 7, "xyz", fmt="markdown", ai_markup="bold:1-2")
    assert s.requests[-1][2]["json"]["ai_markup"] == "bold:1-2"

    await api.send_file(123, "photo", "a.jpg", b"1", caption="c", fmt="markdown", ai_markup="bold:1-2")
    assert s.requests[-1][2]["params"]["ai_markup"] == "bold:1-2"


@pytest.mark.asyncio
async def test_api_parse_mode_overrides_other_format_fields():
    s = _Session()
    api = api_mod.TelegramGatewayApi(s, "http://gw:3333", "u", "p", "+84")

    for pm in ("html", "markdown"):
        await api.send_text(123, "txt", parse_mode=pm, fmt="plain", auto_emphasis=True, ai_markup="bold:1-2")
        body = s.requests[-1][2]["json"]
        assert body["parse_mode"] == pm
        assert "format" not in body and "auto_emphasis" not in body and "ai_markup" not in body

        await api.send_file(123, "photo", "a.jpg", b"1", caption="c", parse_mode=pm, fmt="plain",
                            auto_emphasis=True, ai_markup="bold:1-2")
        params = s.requests[-1][2]["params"]
        assert params["parse_mode"] == pm
        assert "format" not in params and "auto_emphasis" not in params and "ai_markup" not in params

        await api.send_album(123, [("photo", "a.jpg", b"1", "c"), ("photo", "b.jpg", b"2", "")],
                             parse_mode=pm, fmt="plain", auto_emphasis=True, ai_markup="bold:1-2")
        body = s.requests[-1][2]["json"]
        assert body["parse_mode"] == pm
        assert "format" not in body and "auto_emphasis" not in body and "ai_markup" not in body

        await api.edit_message(123, 8, "txt", parse_mode=pm, fmt="plain", auto_emphasis=True, ai_markup="bold:1-2")
        body = s.requests[-1][2]["json"]
        assert body["parse_mode"] == pm
        assert "format" not in body and "auto_emphasis" not in body and "ai_markup" not in body

    # plain_text -> không có parse_mode, và vẫn bỏ format/auto_emphasis/ai_markup
    await api.send_text(123, "txt", parse_mode="plain_text", fmt="markdown", auto_emphasis=True, ai_markup="bold:1-2")
    body = s.requests[-1][2]["json"]
    assert body == {"text": "txt"}

    await api.send_file(123, "photo", "a.jpg", b"1", caption="c", parse_mode="plain_text",
                        fmt="markdown", auto_emphasis=True, ai_markup="bold:1-2")
    params = s.requests[-1][2]["params"]
    assert "parse_mode" not in params and "format" not in params and "auto_emphasis" not in params and "ai_markup" not in params


@pytest.mark.asyncio
async def test_api_send_message_wrapper_forwards_format_args():
    s = _Session()
    api = api_mod.TelegramGatewayApi(s, "http://gw:3333", "u", "p", "+84")

    await api.send_message(456, "hello", reply_to=9, fmt="markdown", auto_emphasis=False, ai_markup="bold:1-2")
    m, url, kw = s.requests[-1]
    assert (m, url) == ("POST", "http://gw:3333/api/chats/456/send")
    assert kw["json"] == {"text": "hello", "reply_to": 9, "format": "markdown",
                          "auto_emphasis": False, "ai_markup": "bold:1-2"}

    await api.send_message(456, "hello", parse_mode="html", fmt="plain", auto_emphasis=True, ai_markup="x")
    assert s.requests[-1][2]["json"] == {"text": "hello", "parse_mode": "html"}


# ── 2. Thực thể cài đặt (DeviceDefaults, TextFormatSelect, AutoEmphasisSwitch, RestoreEntity, PLATFORMS)

def test_device_defaults_initial_values():
    M["saved"] = {}
    dd = devices_mod.DeviceDefaults(types.SimpleNamespace(), "e1")
    assert dd.get(const_mod.CONF_TEXT_FORMAT) == "markdown"
    assert dd.get(const_mod.CONF_TEXT_AUTO_EMPHASIS) is True


@pytest.mark.asyncio
async def test_text_format_select_updates_and_persists():
    M["saved"] = {}
    hass = types.SimpleNamespace()
    dd = devices_mod.DeviceDefaults(hass, "e1")
    rt = types.SimpleNamespace(defaults=dd)
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=rt)

    sel = select_mod.TextFormatSelect(entry, rt)
    assert sel._attr_name == "Chữ: định dạng"
    assert sel.options == ["plain", "markdown"]
    assert sel.current_option == "markdown"

    await sel.async_select_option("plain")
    assert dd.get(const_mod.CONF_TEXT_FORMAT) == "plain"
    assert sel.current_option == "plain"

    dd2 = devices_mod.DeviceDefaults(hass, "e1")
    await dd2.async_load()
    assert dd2.get(const_mod.CONF_TEXT_FORMAT) == "plain"


@pytest.mark.asyncio
async def test_auto_emphasis_switch_updates_and_persists():
    M["saved"] = {}
    hass = types.SimpleNamespace()
    dd = devices_mod.DeviceDefaults(hass, "e1")
    rt = types.SimpleNamespace(defaults=dd)
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=rt)

    sw = switch_mod.AutoEmphasisSwitch(entry, rt)
    assert sw._attr_name == "Chữ: tự nhấn mạnh"
    assert sw.is_on is True

    await sw.async_turn_off()
    assert dd.get(const_mod.CONF_TEXT_AUTO_EMPHASIS) is False
    assert sw.is_on is False

    dd2 = devices_mod.DeviceDefaults(hass, "e1")
    await dd2.async_load()
    assert dd2.get(const_mod.CONF_TEXT_AUTO_EMPHASIS) is False

    await sw.async_turn_on()
    assert dd.get(const_mod.CONF_TEXT_AUTO_EMPHASIS) is True
    assert sw.is_on is True

    dd3 = devices_mod.DeviceDefaults(hass, "e1")
    await dd3.async_load()
    assert dd3.get(const_mod.CONF_TEXT_AUTO_EMPHASIS) is True


@pytest.mark.asyncio
async def test_restore_entity_restores_only_when_store_missing_keys():
    hass = types.SimpleNamespace()

    # Trường hợp 1: Store chưa có khoá text_format / text_auto_emphasis -> khôi phục từ last_state và lưu vào defaults
    M["saved"] = {}
    dd = devices_mod.DeviceDefaults(hass, "e1")
    await dd.async_load()
    rt = types.SimpleNamespace(defaults=dd)
    entry = types.SimpleNamespace(entry_id="e1", runtime_data=rt)

    sel = select_mod.TextFormatSelect(entry, rt)
    sel._fake_last_state = types.SimpleNamespace(state="plain")
    await sel.async_added_to_hass()
    assert dd.get(const_mod.CONF_TEXT_FORMAT) == "plain"

    sw = switch_mod.AutoEmphasisSwitch(entry, rt)
    sw._fake_last_state = types.SimpleNamespace(state="off")
    await sw.async_added_to_hass()
    assert dd.get(const_mod.CONF_TEXT_AUTO_EMPHASIS) is False

    dd_reloaded = devices_mod.DeviceDefaults(hass, "e1")
    await dd_reloaded.async_load()
    assert dd_reloaded.get(const_mod.CONF_TEXT_FORMAT) == "plain"
    assert dd_reloaded.get(const_mod.CONF_TEXT_AUTO_EMPHASIS) is False

    # Trường hợp 2: Store ĐÃ có sẵn giá trị -> async_added_to_hass KHÔNG ghi đè bằng last_state
    M["saved"] = {"telegram_call.devices.e2": {const_mod.CONF_TEXT_FORMAT: "markdown", const_mod.CONF_TEXT_AUTO_EMPHASIS: True}}
    dd2 = devices_mod.DeviceDefaults(hass, "e2")
    await dd2.async_load()
    rt2 = types.SimpleNamespace(defaults=dd2)
    entry2 = types.SimpleNamespace(entry_id="e2", runtime_data=rt2)

    sel2 = select_mod.TextFormatSelect(entry2, rt2)
    sel2._fake_last_state = types.SimpleNamespace(state="plain")
    await sel2.async_added_to_hass()
    assert dd2.get(const_mod.CONF_TEXT_FORMAT) == "markdown"

    sw2 = switch_mod.AutoEmphasisSwitch(entry2, rt2)
    sw2._fake_last_state = types.SimpleNamespace(state="off")
    await sw2.async_added_to_hass()
    assert dd2.get(const_mod.CONF_TEXT_AUTO_EMPHASIS) is True


def test_platforms_in_init_includes_switch():
    init_path = os.path.join(ROOT, "__init__.py")
    with open(init_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    platforms = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "PLATFORMS":
                    platforms = ast.literal_eval(node.value)
    assert isinstance(platforms, list)
    assert "switch" in platforms


# ── 3. Dịch vụ services.py ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_service_send_message_defaults_and_account_changes():
    hass, defaults, session = _setup_services()

    # Mặc định ban đầu: markdown + auto_emphasis=True, không có parse_mode
    await hass.services.async_call("telegram_call", "send_message", {"chat_id": 100, "message": "Cảnh báo"})
    body = session.requests[-1][2]["json"]
    assert body == {"text": "Cảnh báo", "format": "markdown", "auto_emphasis": True}

    # Đổi cài đặt tài khoản sang plain + auto_emphasis=False
    await defaults.async_set(text_format="plain", text_auto_emphasis=False)
    await hass.services.async_call("telegram_call", "send_message", {"chat_id": 100, "message": "Thử nghiệm"})
    body = session.requests[-1][2]["json"]
    assert body == {"text": "Thử nghiệm", "format": "plain", "auto_emphasis": False}

    # Truyền format="default" -> lấy theo cài đặt tài khoản
    await hass.services.async_call("telegram_call", "send_message", {"chat_id": 100, "message": "Theo mặc định", "format": "default"})
    assert session.requests[-1][2]["json"] == {"text": "Theo mặc định", "format": "plain", "auto_emphasis": False}


@pytest.mark.asyncio
async def test_service_send_message_overrides_and_parse_mode():
    hass, defaults, session = _setup_services()

    # Cài đặt markdown + BẬT; truyền format="plain" và auto_emphasis=False -> ghi đè đúng
    await hass.services.async_call("telegram_call", "send_message", {
        "chat_id": 100, "message": "Ghi đè", "format": "plain", "auto_emphasis": False,
    })
    assert session.requests[-1][2]["json"] == {"text": "Ghi đè", "format": "plain", "auto_emphasis": False}

    # Cài đặt TẮT; truyền auto_emphasis=True -> ghi đè sang True
    await defaults.async_set(text_auto_emphasis=False)
    await hass.services.async_call("telegram_call", "send_message", {
        "chat_id": 100, "message": "Bật lại", "auto_emphasis": True,
    })
    assert session.requests[-1][2]["json"] == {"text": "Bật lại", "format": "markdown", "auto_emphasis": True}

    # parse_mode: html / markdown / MarkdownV2 / plain_text -> KHÔNG kèm format / auto_emphasis / ai_markup
    for raw_pm, expected_pm in (("html", "html"), ("markdown", "markdown"), ("MarkdownV2", "markdown"), ("plain_text", None)):
        await hass.services.async_call("telegram_call", "send_message", {
            "chat_id": 100, "message": "PM", "parse_mode": raw_pm, "format": "plain",
            "auto_emphasis": True, "ai_markup": "bold:1-2",
        })
        body = session.requests[-1][2]["json"]
        assert "format" not in body and "auto_emphasis" not in body and "ai_markup" not in body
        if expected_pm is None:
            assert "parse_mode" not in body
        else:
            assert body["parse_mode"] == expected_pm

    # format="html" -> body có parse_mode="html", không có format / auto_emphasis
    await hass.services.async_call("telegram_call", "send_message", {
        "chat_id": 100, "message": "HTML", "format": "html", "auto_emphasis": True,
    })
    assert session.requests[-1][2]["json"] == {"text": "HTML", "parse_mode": "html"}


@pytest.mark.asyncio
async def test_service_send_photo_media_group_and_edit_message(tmp_path):
    hass, defaults, session = _setup_services()
    p1 = tmp_path / "a.jpg"
    p2 = tmp_path / "b.jpg"
    p1.write_bytes(b"img1")
    p2.write_bytes(b"img2")

    # send_photo có caption: không truyền format -> theo cài đặt; truyền parse_mode -> chỉ parse_mode
    await hass.services.async_call("telegram_call", "send_photo", {
        "chat_id": 200, "file": str(p1), "caption": "Ảnh 1",
    })
    params = session.requests[-1][2]["params"]
    assert params["format"] == "markdown" and params["auto_emphasis"] == "true" and "parse_mode" not in params

    await hass.services.async_call("telegram_call", "send_photo", {
        "chat_id": 200, "file": str(p1), "caption": "Ảnh 1", "parse_mode": "html",
    })
    params = session.requests[-1][2]["params"]
    assert params["parse_mode"] == "html" and "format" not in params and "auto_emphasis" not in params

    # send_media_group album 2 ảnh: không truyền -> theo cài đặt; truyền parse_mode -> chỉ parse_mode
    media = [{"media_type": "photo", "file": str(p1), "caption": "A"},
             {"media_type": "photo", "file": str(p2), "caption": "B"}]
    await hass.services.async_call("telegram_call", "send_media_group", {"chat_id": 200, "media": media})
    assert session.requests[-1][1].endswith("/api/chats/200/send_album")
    body = session.requests[-1][2]["json"]
    assert body["format"] == "markdown" and body["auto_emphasis"] is True and "parse_mode" not in body

    await hass.services.async_call("telegram_call", "send_media_group", {
        "chat_id": 200, "media": media, "parse_mode": "html",
    })
    body = session.requests[-1][2]["json"]
    assert body["parse_mode"] == "html" and "format" not in body and "auto_emphasis" not in body

    # edit_message: không truyền -> theo cài đặt; truyền parse_mode -> chỉ parse_mode
    await hass.services.async_call("telegram_call", "edit_message", {
        "chat_id": 200, "message_id": 77, "message": "Đã sửa",
    })
    body = session.requests[-1][2]["json"]
    assert body == {"chat_id": 200, "message_id": 77, "text": "Đã sửa", "format": "markdown", "auto_emphasis": True}

    await hass.services.async_call("telegram_call", "edit_message", {
        "chat_id": 200, "message_id": 77, "message": "Đã sửa", "parse_mode": "markdown",
    })
    body = session.requests[-1][2]["json"]
    assert body == {"chat_id": 200, "message_id": 77, "text": "Đã sửa", "parse_mode": "markdown"}


@pytest.mark.asyncio
async def test_service_target_list_and_invalid_format():
    hass, defaults, session = _setup_services()

    # target=[-100777, -100888] (mẫu calendar_weather_notification) không truyền format
    await hass.services.async_call("telegram_call", "send_message", {
        "target": [-100777, -100888], "message": "Dự báo thời tiết",
    })
    _, url, kw = session.requests[-1]
    assert "/api/chats/-100777/send" in url
    assert kw["json"] == {"text": "Dự báo thời tiết", "format": "markdown", "auto_emphasis": True}

    # format="bbcode" -> ném vol.Invalid
    with pytest.raises(vol.Invalid):
        await hass.services.async_call("telegram_call", "send_message", {
            "chat_id": 100, "message": "Sai định dạng", "format": "bbcode",
        })
