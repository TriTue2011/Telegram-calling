"""Luồng tạo nhiều tài khoản qua gateway giả, kể cả danh sách trống và tài khoản mất online."""
import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from test_ha_end_to_end import DOMAIN, TOKEN, gw  # noqa: F401


async def test_config_flow_selects_each_online_account_and_rejects_duplicates(hass, gw, enable_custom_integrations):
    from unittest.mock import AsyncMock, patch

    gw.accounts = [{"phone": "+84900000000", "display_name": "Nhà", "status": "online"},
                   {"phone": "+84900000001", "display_name": "Cơ quan", "status": "online"},
                   {"phone": "+84900000002", "status": "offline"}]
    with patch("custom_components.telegram_call.async_setup_entry", new=AsyncMock(return_value=True)):
        for phone in ("+84900000000", "+84900000001"):
            form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
            pick = await hass.config_entries.flow.async_configure(form["flow_id"], {"url": gw.url, "token": TOKEN})
            assert pick["type"] == "form" and pick["step_id"] == "account"
            selector = next(iter(pick["data_schema"].schema.values()))
            assert {o["value"] for o in selector.config["options"]} == {"+84900000000", "+84900000001"}
            done = await hass.config_entries.flow.async_configure(form["flow_id"], {"account": phone})
            assert done["type"] == "create_entry" and done["data"]["account"] == phone
            await hass.async_block_till_done()
        form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        await hass.config_entries.flow.async_configure(form["flow_id"], {"url": gw.url, "token": TOKEN})
        done = await hass.config_entries.flow.async_configure(form["flow_id"], {"account": "+84900000000"})
        assert done["type"] == "abort" and done["reason"] == "already_configured"


@pytest.mark.parametrize("rows", [[], [{"phone": "+84900000000", "status": "offline"}]])
async def test_config_flow_reports_no_online_accounts(hass, gw, enable_custom_integrations, rows):
    gw.accounts = rows
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"url": gw.url, "token": TOKEN})
    assert result["type"] == "form" and result["errors"] == {"base": "no_account"}


async def test_account_removed_between_form_and_submit_is_refused(hass, gw, enable_custom_integrations):
    gw.accounts = [{"phone": "+84900000000", "status": "online"}, {"phone": "+84900000001", "status": "online"}]
    form = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    await hass.config_entries.flow.async_configure(form["flow_id"], {"url": gw.url, "token": TOKEN})
    gw.accounts.pop()
    result = await hass.config_entries.flow.async_configure(form["flow_id"], {"account": "+84900000001"})
    assert result["type"] == "form" and result["errors"] == {"base": "account_unavailable"}
