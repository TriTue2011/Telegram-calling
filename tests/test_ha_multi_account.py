"""Hai mục cấu hình và gateway hai tài khoản, gọi qua dịch vụ HA thật."""
import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from pytest_homeassistant_custom_component.common import MockConfigEntry
from homeassistant.exceptions import ServiceValidationError
from test_ha_end_to_end import DOMAIN, TOKEN, FakeGateway, call

PHONE_A, PHONE_B = "+84900000000", "+84900000001"


@pytest.fixture
async def two_accounts(hass, socket_enabled, enable_custom_integrations):
    gw = FakeGateway()
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
    await hass.config_entries.async_reload(first.entry_id)
    await hass.async_block_till_done()
    await call(hass, "send_message", {"chat_id": 42, "message": "sau reload"})
    assert gw.last("/send")["query"]["account"] == PHONE_A


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
