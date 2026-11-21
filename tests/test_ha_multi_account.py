"""Hai mục cấu hình và gateway hai tài khoản, gọi qua dịch vụ HA thật."""
import asyncio
import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from aiohttp import web
from pytest_homeassistant_custom_component.common import async_capture_events
from test_ha_end_to_end import DOMAIN, TOKEN, FakeGateway, call

PHONE_A, PHONE_B = "+84900000000", "+84900000001"


class MultiGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def _any(self, request):
        if request.path == "/api/live/calls":
            # Cố ý trả gộp để kiểm tích hợp vẫn lọc theo account ở biên API.
            self.requests.append({"path": request.path, "query": dict(request.query)})
            return web.json_response({"calls": self.calls})
        if request.path == "/api/live/accept":
            body = await request.json()
            self.requests.append({"path": request.path, "query": dict(request.query), "json": body})
            return web.json_response({"id": body["id"], "state": "active"})
        return await super()._any(request)


@pytest.fixture
async def two_accounts(hass, socket_enabled, enable_custom_integrations):
    gw = MultiGateway()
    gw.accounts = [{"phone": PHONE_A, "display_name": "Nhà", "status": "online"},
                   {"phone": PHONE_B, "display_name": "Cơ quan", "status": "online"}]
    await gw.start()
    entries = []
    try:
        for phone in (PHONE_A, PHONE_B):
            entry = MockConfigEntry(domain=DOMAIN, data={"url": gw.url, "token": TOKEN, "account": phone}, title=phone)
            entry.add_to_hass(hass)
            assert await hass.config_entries.async_setup(entry.entry_id)
            entries.append(entry)
            await hass.async_block_till_done()
        yield gw, entries
    finally:
        for entry in reversed(entries):
            await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        await gw.stop()


async def test_legacy_services_with_two_entries_use_default(hass, two_accounts):
    gw, (first, second) = two_accounts
    await call(hass, "send_message", {"chat_id": 42, "message": "automation cũ"})
    assert gw.last("/send")["query"]["account"] == PHONE_A
    await call(hass, "apply_call_rule", {"call_id": "old", "answer": False})
    assert "old" in first.runtime_data.voice.claims
    assert "old" not in second.runtime_data.voice.claims
    gw.calls = [{"id": "a-call", "account": PHONE_A, "state": "ringing", "outgoing": False},
                {"id": "b-call", "account": PHONE_B, "state": "ringing", "outgoing": False}]
    await asyncio.gather(*(e.runtime_data.coordinator.async_refresh() for e in (first, second)))
    await call(hass, "answer_call", {})
    assert gw.last("/api/live/accept")["query"]["account"] == PHONE_A
    assert gw.last("/api/live/accept")["json"] == {"id": "a-call"}
    await hass.config_entries.async_reload(first.entry_id)
    await hass.async_block_till_done()
    await call(hass, "send_message", {"chat_id": 42, "message": "sau reload"})
    assert gw.last("/send")["query"]["account"] == PHONE_A


async def test_two_entries_isolate_simultaneous_calls_messages_and_notify(hass, two_accounts):
    gw, (first, second) = two_accounts
    call_events = async_capture_events(hass, "telegram_call_call_state")
    message_events = async_capture_events(hass, "telegram_call_message")
    for entry in (first, second):
        entry.runtime_data.voice.decide_grace = 60
    gw.calls = [{"id": "same", "account": phone, "state": "ringing", "outgoing": False, "user_id": uid}
                for phone, uid in ((PHONE_A, 1), (PHONE_B, 2))]
    await asyncio.gather(*(e.runtime_data.coordinator.async_refresh() for e in (first, second)))
    await hass.async_block_till_done()
    assert first.runtime_data.coordinator.live_calls()[0]["user_id"] == 1
    assert second.runtime_data.coordinator.live_calls()[0]["user_id"] == 2
    assert {(e.data["account"], e.data["user_id"]) for e in call_events} == {(PHONE_A, 1), (PHONE_B, 2)}
    for phone, text in ((PHONE_A, "tin A"), (PHONE_B, "tin B")):
        item = {"type": "message", "account": phone, "message": {"id": 1, "chat_id": 42, "text": text}}
        first.runtime_data.messages.handle(item)
        second.runtime_data.messages.handle(item)
    await hass.async_block_till_done()
    assert first.runtime_data.messages.last["text"] == "tin A" and first.runtime_data.messages.received == 1
    assert second.runtime_data.messages.last["text"] == "tin B" and second.runtime_data.messages.received == 1
    assert len(message_events) == 2
    await call(hass, "apply_call_rule", {"account": PHONE_B, "call_id": "same", "source": "automation.b", "answer": False})
    assert "same" not in first.runtime_data.voice.claims and "same" in second.runtime_data.voice.claims
    eid = er.async_get(hass).async_get_entity_id("notify", DOMAIN, f"{second.entry_id}_to_1")
    await hass.services.async_call("notify", "send_message", {"entity_id": eid, "message": "gửi từ B"}, blocking=True)
    assert gw.last("/send")["query"]["account"] == PHONE_B


async def test_two_background_feeds_do_not_mix_aggregated_gateway_items(hass, two_accounts):
    gw, (first, second) = two_accounts
    events = async_capture_events(hass, "telegram_call_message")
    await asyncio.sleep(0.3)
    gw.feed_items += [{"id": index, "type": "message", "account": phone,
                       "message": {"id": 21, "chat_id": 42, "outgoing": False, "text": text}}
                      for index, phone, text in ((2, PHONE_A, "tin A"), (3, PHONE_B, "tin B"))]
    for _ in range(40):
        if len(events) >= 2:
            break
        await asyncio.sleep(0.1)
    assert len(events) == 2
    assert {(e.data["account"], e.data["text"]) for e in events} == {(PHONE_A, "tin A"), (PHONE_B, "tin B")}
    assert first.runtime_data.messages.received == second.runtime_data.messages.received == 1


async def test_options_switches_default_for_legacy_service(hass, two_accounts):
    gw, (first, second) = two_accounts
    form = await hass.config_entries.options.async_init(second.entry_id)
    result = await hass.config_entries.options.async_configure(form["flow_id"], {"default_account": second.entry_id, "recipients": []})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    await call(hass, "send_message", {"chat_id": 42, "message": "mặc định mới"})
    assert gw.last("/send")["query"]["account"] == PHONE_B


async def test_services_select_by_phone_name_and_entry_id_priority(hass, two_accounts):
    gw, (first, second) = two_accounts
    for account in (PHONE_B, "Cơ quan"):
        await call(hass, "send_message", {"account": account, "chat_id": 42, "message": "x"})
        assert gw.last("/send")["query"]["account"] == PHONE_B
    await call(hass, "send_message", {"config_entry_id": first.entry_id, "account": PHONE_B, "chat_id": 42, "message": "x"})
    assert gw.last("/send")["query"]["account"] == PHONE_A
    before = len(gw.requests)
    with pytest.raises(ServiceValidationError, match="Không tìm thấy tài khoản"):
        await call(hass, "send_message", {"account": "không có", "chat_id": 42, "message": "x"})
    assert len(gw.requests) == before
    await call(hass, "get_me", {"account": "Cơ quan"})
    req = gw.last("/api/do/get_me")
    assert req["query"]["account"] == PHONE_B and req["json"] == {}


async def test_profile_rename_updates_device_without_changing_existing_entity_ids(hass, two_accounts):
    gw, (first, second) = two_accounts
    devices, entities = dr.async_get(hass), er.async_get(hass)
    device = devices.async_get_device(identifiers={(DOMAIN, first.entry_id)})
    assert device.name == "Telegram Call — Nhà"
    assert devices.async_get_device(identifiers={(DOMAIN, second.entry_id)}).name == "Telegram Call — Cơ quan"
    eid = entities.async_get_entity_id("sensor", DOMAIN, f"{first.entry_id}_call_state")
    entities.async_update_entity(eid, new_entity_id="sensor.telegram_call_call_state")
    before = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(entities, first.entry_id)}
    gw.accounts[0]["display_name"] = "Tên mới"
    await first.runtime_data.account.async_refresh()
    await hass.async_block_till_done()
    assert devices.async_get(device.id).name == "Telegram Call — Tên mới"
    after = {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(entities, first.entry_id)}
    assert before == after
    await hass.config_entries.async_reload(first.entry_id)
    await hass.async_block_till_done()
    assert before == {e.unique_id: e.entity_id for e in er.async_entries_for_config_entry(entities, first.entry_id)}
    await call(hass, "send_message", {"account": "Tên mới", "chat_id": 42, "message": "x"})
    assert gw.last("/send")["query"]["account"] == PHONE_A
