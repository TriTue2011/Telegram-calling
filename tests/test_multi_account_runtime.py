"""Chạy logic runtime thật với các biên HA giả; không thay thế kiểm thử HA đầu-cuối."""
import ast
import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, parse_qs, urlsplit

from test_account_policy import accounts

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "telegram_call"


def definitions(filename, names, **namespace):
    """Nạp định nghĩa nguyên bản, chỉ tiêm biên framework để không cần cài HA."""
    path = ROOT / filename
    tree = ast.parse(path.read_text())
    nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
    assert {n.name for n in nodes} == set(names)
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


class Coordinator:
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, **kwargs):
        self.hass, self.config_entry = hass, kwargs["config_entry"]


def test_account_poll_renames_only_its_device_and_preserves_identifiers():
    registry_calls = []
    device = SimpleNamespace(id="device-a", name="Telegram Call")
    registry = SimpleNamespace(
        async_get_device=lambda **kwargs: device if kwargs["identifiers"] == {("telegram_call", "entry-a")} else None,
        async_update_device=lambda key, **kwargs: registry_calls.append((key, kwargs)),
    )
    module = definitions("__init__.py", ["AccountCoordinator"], DataUpdateCoordinator=Coordinator, _LOGGER=None,
                         DOMAIN="telegram_call", ACCOUNT_POLL_S=60, timedelta=timedelta, CONF_ACCOUNT="account",
                         dr=SimpleNamespace(async_get=lambda hass: registry), device_name=accounts.device_name)
    rows = [{"phone": "+84900000001", "display_name": "Khác", "status": "online"},
            {"phone": "+84900000000", "display_name": "Nhà", "status": "online"}]

    async def get_accounts():
        return rows

    entry = SimpleNamespace(entry_id="entry-a", data={"account": "+84900000000"})
    coordinator = module.AccountCoordinator(object(), entry, SimpleNamespace(accounts=get_accounts))
    result = asyncio.run(coordinator._async_update_data())
    assert result == {"phone": "+84900000000", "display_name": "Nhà", "status": "online"}
    assert registry_calls == [("device-a", {"name": "Telegram Call — Nhà"})]
    rows[1]["display_name"] = "Tên mới"
    asyncio.run(coordinator._async_update_data())
    assert registry_calls[-1] == ("device-a", {"name": "Telegram Call — Tên mới"})
    rows.pop()
    assert asyncio.run(coordinator._async_update_data())["status"] == "missing"
    assert registry_calls[-1] == ("device-a", {"name": "Telegram Call — +84…000"})


def test_device_info_uses_current_profile_and_entity_unique_id_is_stable():
    module = definitions("entity.py", ["device_info", "TelegramCallEntity"], DeviceInfo=dict, Entity=object,
                         DOMAIN="telegram_call", CONF_ACCOUNT="account", device_name=accounts.device_name)
    data = {"display_name": "Nhà"}
    entry = SimpleNamespace(entry_id="entry-a", data={"account": "+84900000000"},
                            runtime_data=SimpleNamespace(account=SimpleNamespace(data=data)))
    entity = module.TelegramCallEntity(entry, "call_state")
    assert entity._attr_unique_id == "entry-a_call_state"
    assert entity.device_info["identifiers"] == {("telegram_call", "entry-a")}
    data["display_name"] = "Tên mới"
    assert entity.device_info["name"] == "Telegram Call — Tên mới"
    assert entity._attr_unique_id == "entry-a_call_state"


def call_coordinator(phone):
    core = definitions("bridge_core.py", ["derive_state"])
    module = definitions("__init__.py", ["TelegramCallCoordinator"], DataUpdateCoordinator=Coordinator, _LOGGER=None,
                         DOMAIN="telegram_call", POLL_IDLE_S=1.5, timedelta=timedelta, derive_state=core.derive_state,
                         redact=lambda text: text, TERMINAL=("ended", "not_answered", "missed", "error"))
    return module.TelegramCallCoordinator(object(), SimpleNamespace(), SimpleNamespace(account=phone))


def test_call_and_video_request_events_keep_old_fields_and_add_account():
    coordinator = call_coordinator("+84900000000")
    call = {"id": "c1", "user_id": 5, "outgoing": False, "video": False, "state": "ringing", "account": "+84900000000"}
    event = asyncio.run(coordinator._on_calls([call]))[0]
    assert event["account"] == "+84900000000" and event["state"] == "incoming" and event["call_id"] == "c1"
    events = asyncio.run(coordinator._on_calls([{**call, "state": "active", "peer_video": True}]))
    assert {e["state"] for e in events} == {"answered", "video_request"}
    assert all(e["account"] == "+84900000000" for e in events)


def message_feed(phone, events):
    core = definitions("bridge_core.py", ["message_event", "send_failed_event"], redact=lambda text: text)
    module = definitions("messages.py", ["MessageFeed"], message_event=core.message_event, send_failed_event=core.send_failed_event,
                         EVENT_MESSAGE="telegram_call_message", EVENT_SEND_FAILED="telegram_call_send_failed", SIGNAL_MESSAGE="message_{}",
                         async_dispatcher_send=lambda *args: None)
    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=lambda event, data: events.append((event, data))))
    return module.MessageFeed(hass, SimpleNamespace(account=phone), "entry")


def test_message_and_failure_events_add_account_with_legacy_feed_fallback():
    events = []
    feed = message_feed("+84900000000", events)
    feed.handle({"type": "message", "message": {"id": 1, "chat_id": 42, "text": "xin chào"}})
    feed.handle({"type": "send_failed", "chat_id": 42, "old_id": 9, "error": "lỗi", "account": "+84900000000"})
    assert len(events) == 2 and all(e[1]["account"] == "+84900000000" for e in events)
    assert events[0][1]["text"] == "xin chào" and events[1][1]["old_message_id"] == 9
    assert feed.received == 1


def test_config_gateway_check_only_offers_signed_in_accounts():
    rows = [{"phone": "+84900000000", "status": "online"}, {"phone": "+84900000001", "status": "offline"},
            {"phone": "+84900000002", "status": "wait_code"}, {"phone": "+84900000003", "status": "mock"}]

    class Api:
        def __init__(self, *args):
            pass

        async def health(self):
            return {}

        async def accounts(self):
            return rows

    module = definitions("config_flow.py", ["_check"], TelegramGatewayApi=Api, async_get_clientsession=lambda hass: None)
    error, result = asyncio.run(module._check(object(), "http://192.0.2.10", "TOKEN-GIA"))
    assert error is None and result == [rows[0], rows[3]]
    rows.clear()
    assert asyncio.run(module._check(object(), "http://192.0.2.10", "TOKEN-GIA")) == (None, [])


def test_options_switch_clears_unloaded_default_and_preserves_other_options():
    class Flow:
        def async_create_entry(self, **kwargs):
            return kwargs

    module = definitions("config_flow.py", ["TelegramCallOptionsFlow"], OptionsFlow=Flow,
                         DOMAIN="telegram_call", CONF_DEFAULT="default_account", default_entry=accounts.default_entry)
    offline = SimpleNamespace(entry_id="offline", options={"default_account": True, "recipients": ["9"]})

    async def contacts():
        return []

    online = SimpleNamespace(entry_id="online", options={"recipients": []},
                             runtime_data=SimpleNamespace(api=SimpleNamespace(contacts=contacts), account=SimpleNamespace(data={"status": "online"})))
    changes = []
    entries = SimpleNamespace(async_loaded_entries=lambda domain: [online], async_entries=lambda domain: [offline, online],
                              async_update_entry=lambda entry, **kwargs: changes.append((entry.entry_id, kwargs)))
    flow = module.TelegramCallOptionsFlow()
    flow.hass, flow.config_entry = SimpleNamespace(config_entries=entries), online
    result = asyncio.run(flow.async_step_init({"default_account": "online", "recipients": []}))
    assert result["data"] == {"default_account": True, "recipients": []}
    assert changes == [("offline", {"options": {"default_account": False, "recipients": ["9"]}})]


def test_two_message_feeds_never_accept_each_others_messages_or_failures():
    a_events, b_events = [], []
    a, b = message_feed("+84900000000", a_events), message_feed("+84900000001", b_events)
    for phone, text in (("+84900000000", "tin A"), ("+84900000001", "tin B")):
        for item in ({"type": "message", "account": phone, "message": {"id": 1, "chat_id": 42, "text": text}},
                     {"type": "send_failed", "account": phone, "old_id": 1}):
            a.handle(item)
            b.handle(item)
    assert a.received == b.received == 1
    assert a.last["text"] == "tin A" and b.last["text"] == "tin B"
    assert len(a_events) == len(b_events) == 2
    assert all(e[1]["account"] == "+84900000000" for e in a_events)
    assert all(e[1]["account"] == "+84900000001" for e in b_events)


class Response:
    status = 200

    def __init__(self, payload=None):
        self.payload = payload or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return self.payload

    async def read(self):
        return b"TEP-GIA"


class Session:
    def __init__(self):
        self.requests = []
        self.calls = [{"id": "same", "account": phone, "state": "ringing"} for phone in ("+84900000000", "+84900000001")]

    def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return Response({"calls": self.calls})

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


def api_module():
    return definitions("api.py", ["TelegramCallError", "TelegramCallAuthError", "TelegramGatewayApi"],
                       TIMEOUT=None, asyncio=asyncio, quote=quote, redact=lambda text: text,
                       aiohttp=SimpleNamespace(ClientTimeout=lambda **kwargs: None, ClientError=OSError, ContentTypeError=ValueError))


def test_api_scopes_calls_feed_upload_download_and_view_for_two_accounts():
    session = Session()
    module = api_module()

    async def run():
        for phone in ("+84900000000", "+84900000001"):
            api = module.TelegramGatewayApi(session, "http://192.0.2.10:3333", "TOKEN-GIA", phone)
            calls = await api.live_calls()
            assert len(calls) == 1 and calls[0]["account"] == phone
            await api.feed(9)
            await api.accept("same")
            await api.set_live_settings({"mic": "silence"})
            await api.send_file(42, "photo", "a.jpg", b"photo")
            await api.send_voice_tts("42", "xin chào")
            assert await api.download_file(1) == b"TEP-GIA"
            for _, _, kwargs in session.requests[-7:]:
                assert kwargs["params"]["account"] == phone
            assert parse_qs(urlsplit(api.view_url("same")).query) == {"account": [phone]}
    asyncio.run(run())


def test_two_call_coordinators_with_identical_call_ids_keep_separate_state():
    module = api_module()
    session = Session()
    a, b = call_coordinator("+84900000000"), call_coordinator("+84900000001")

    async def run():
        clients = [module.TelegramGatewayApi(session, "http://192.0.2.10", "TOKEN-GIA", c.api.account) for c in (a, b)]
        rows = await asyncio.gather(*(api.live_calls() for api in clients))
        events = await asyncio.gather(a._on_calls(rows[0]), b._on_calls(rows[1]))
        assert events[0][0]["account"] == "+84900000000" and events[1][0]["account"] == "+84900000001"
        session.calls[0]["state"] = "active"
        rows = await asyncio.gather(*(api.live_calls() for api in clients))
        assert (await a._on_calls(rows[0]))[0]["state"] == "answered"
        assert await b._on_calls(rows[1]) == []
        assert a._was_active == {"same"} and b._was_active == set()
    asyncio.run(run())


def test_notify_entity_sends_through_its_own_entry_even_when_default_is_different():
    module = definitions("notify.py", ["TelegramRecipient"], NotifyEntity=object, DOMAIN="telegram_call")
    sent = []

    async def service(*args, **kwargs):
        sent.append((args, kwargs))

    entity = module.TelegramRecipient(SimpleNamespace(entry_id="entry-b"), "42", "Người nhận")
    entity.hass = SimpleNamespace(services=SimpleNamespace(async_call=service))
    asyncio.run(entity.async_send_message("tin B"))
    assert sent == [(("telegram_call", "send_message", {"config_entry_id": "entry-b", "peer": "42", "message": "tin B"}), {"blocking": True})]
