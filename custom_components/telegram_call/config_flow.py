"""Luồng cấu hình: địa chỉ gateway + token, chọn tài khoản Telegram (nếu gateway có nhiều), đổi token / địa chỉ về sau."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelegramCallAuthError, TelegramCallError, TelegramGatewayApi
from .const import CONF_ACCOUNT, CONF_TOKEN, CONF_URL, DOMAIN

_TOKEN = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


def _normalize_url(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    return raw if raw.startswith(("http://", "https://")) else "http://" + raw


async def _check(hass, url: str, token: str) -> tuple[str | None, list[dict]]:
    """Thử nối gateway. (None, danh_sách_tài_khoản) nếu được; ngược lại (khoá lỗi của form, [])."""
    api = TelegramGatewayApi(async_get_clientsession(hass), url, token)
    try:
        await api.health()
        accounts = await api.accounts()
    except TelegramCallAuthError:
        return "invalid_auth", []
    except TelegramCallError:
        return "cannot_connect", []
    return None, accounts


class TelegramCallConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._url = self._token = ""
        self._accounts: list[dict] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            err, accounts = await _check(self.hass, url, user_input[CONF_TOKEN])
            if err:
                errors["base"] = err
            elif not accounts:
                errors["base"] = "no_account"
            else:
                self._url, self._token, self._accounts = url, user_input[CONF_TOKEN], accounts
                if len(accounts) == 1:
                    return await self._create(accounts[0]["phone"])
                return await self.async_step_account()
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_URL, default="http://172.16.10.200:3333"): str, vol.Required(CONF_TOKEN): _TOKEN}))

    async def async_step_account(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return await self._create(user_input[CONF_ACCOUNT])
        opts = [selector.SelectOptionDict(value=a["phone"], label=f"{a.get('display_name') or a['phone']} ({a['phone']}, {a.get('status', '?')})")
                for a in self._accounts]
        return self.async_show_form(step_id="account", data_schema=vol.Schema({
            vol.Required(CONF_ACCOUNT): selector.SelectSelector(selector.SelectSelectorConfig(options=opts))}))

    async def _create(self, phone: str) -> ConfigFlowResult:
        await self.async_set_unique_id(f"{self._url}|{phone}")
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=f"Telegram Call ({phone})",
                                       data={CONF_URL: self._url, CONF_TOKEN: self._token, CONF_ACCOUNT: phone})

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            err, _ = await _check(self.hass, entry.data[CONF_URL], user_input[CONF_TOKEN])
            if err:
                errors["base"] = err
            else:
                return self.async_update_reload_and_abort(entry, data_updates={CONF_TOKEN: user_input[CONF_TOKEN]})
        return self.async_show_form(step_id="reauth_confirm", errors=errors, data_schema=vol.Schema({vol.Required(CONF_TOKEN): _TOKEN}),
                                    description_placeholders={"url": entry.data[CONF_URL]})

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            err, _ = await _check(self.hass, url, user_input[CONF_TOKEN])
            if err:
                errors["base"] = err
            else:
                self._url = url
                new_id = f"{url}|{entry.data[CONF_ACCOUNT]}"
                if new_id != entry.unique_id:
                    await self.async_set_unique_id(new_id)
                    self._abort_if_unique_id_configured()
                return self.async_update_reload_and_abort(entry, unique_id=new_id,
                                                          data_updates={CONF_URL: url, CONF_TOKEN: user_input[CONF_TOKEN]})
        return self.async_show_form(step_id="reconfigure", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_URL, default=entry.data[CONF_URL]): str, vol.Required(CONF_TOKEN): _TOKEN}))
