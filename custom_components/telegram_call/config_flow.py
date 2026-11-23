"""Luồng cấu hình: địa chỉ gateway + TÊN ĐĂNG NHẬP + MẬT KHẨU web của gateway (không dùng token), chọn tài khoản Telegram (nếu gateway có nhiều), đổi thông tin về sau."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelegramCallAuthError, TelegramCallError, TelegramGatewayApi
from .const import CONF_ACCOUNT, CONF_PASSWORD, CONF_URL, CONF_USERNAME, DOMAIN

_PASSWORD = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))
_USERNAME = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT, autocomplete="username"))


def _normalize_url(raw: str) -> str:
    raw = raw.strip().rstrip("/")
    return raw if raw.startswith(("http://", "https://")) else "http://" + raw


async def _check(hass, url: str, username: str, password: str) -> tuple[str | None, list[dict]]:
    """Thử nối gateway. (None, danh_sách_tài_khoản) nếu được; ngược lại (khoá lỗi của form, [])."""
    api = TelegramGatewayApi(async_get_clientsession(hass), url, username, password)
    try:
        await api.health()
        await api.login()
        accounts = await api.accounts()
    except TelegramCallAuthError:
        return "invalid_auth", []
    except TelegramCallError:
        return "cannot_connect", []
    return None, accounts


class TelegramCallConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TelegramCallOptionsFlow()

    def __init__(self) -> None:
        self._url = self._username = self._password = ""
        self._accounts: list[dict] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            err, accounts = await _check(self.hass, url, user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            if err:
                errors["base"] = err
            elif not accounts:
                errors["base"] = "no_account"
            else:
                self._url, self._username, self._password, self._accounts = url, user_input[CONF_USERNAME], user_input[CONF_PASSWORD], accounts
                first = accounts[0]["phone"]
                self._add_other_accounts(accounts, first)     # nhiều tài khoản ⇒ thêm hết, không bắt chọn từng cái
                return await self._create(first)
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_URL, default="http://<máy-gateway>:3333"): str, vol.Required(CONF_USERNAME): _USERNAME, vol.Required(CONF_PASSWORD): _PASSWORD}))

    async def async_step_import(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Tạo mục cho MỘT tài khoản Telegram (gọi tự động cho các tài khoản còn lại của cùng gateway)."""
        self._url, self._username, self._password = data[CONF_URL], data[CONF_USERNAME], data[CONF_PASSWORD]
        return await self._create(data[CONF_ACCOUNT])

    def _add_other_accounts(self, accounts: list[dict], skip: str) -> None:
        """Gateway có nhiều tài khoản Telegram thì mỗi tài khoản là MỘT mục (một thiết bị) — thêm nốt các tài khoản chưa có,
        không bắt người dùng chạy lại luồng thêm tích hợp cho từng tài khoản."""
        for acc in accounts:
            phone = acc.get("phone")
            if not phone or phone == skip:
                continue
            if any(e.data.get(CONF_URL) == self._url and e.data.get(CONF_ACCOUNT) == phone for e in self._async_current_entries(include_ignore=False)):
                continue
            self.hass.async_create_task(self.hass.config_entries.flow.async_init(
                DOMAIN, context={"source": "import"},
                data={CONF_URL: self._url, CONF_USERNAME: self._username, CONF_PASSWORD: self._password, CONF_ACCOUNT: phone}))

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
                                       data={CONF_URL: self._url, CONF_USERNAME: self._username, CONF_PASSWORD: self._password, CONF_ACCOUNT: phone})

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            err, accounts = await _check(self.hass, entry.data[CONF_URL], user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            if err:
                errors["base"] = err
            else:
                self._url, self._username, self._password = entry.data[CONF_URL], user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                self._add_other_accounts(accounts, entry.data.get(CONF_ACCOUNT, ""))     # mục cũ chỉ có 1 tài khoản: thêm các tài khoản còn lại
                return self.async_update_reload_and_abort(entry, data_updates={CONF_USERNAME: user_input[CONF_USERNAME], CONF_PASSWORD: user_input[CONF_PASSWORD]})
        return self.async_show_form(step_id="reauth_confirm", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): _USERNAME, vol.Required(CONF_PASSWORD): _PASSWORD}),
                                    description_placeholders={"url": entry.data[CONF_URL]})

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            url = _normalize_url(user_input[CONF_URL])
            err, _ = await _check(self.hass, url, user_input[CONF_USERNAME], user_input[CONF_PASSWORD])
            if err:
                errors["base"] = err
            else:
                self._url = url
                new_id = f"{url}|{entry.data[CONF_ACCOUNT]}"
                if new_id != entry.unique_id:
                    await self.async_set_unique_id(new_id)
                    self._abort_if_unique_id_configured()
                return self.async_update_reload_and_abort(entry, unique_id=new_id,
                                                          data_updates={CONF_URL: url, CONF_USERNAME: user_input[CONF_USERNAME], CONF_PASSWORD: user_input[CONF_PASSWORD]})
        return self.async_show_form(step_id="reconfigure", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_URL, default=entry.data[CONF_URL]): str,
            vol.Required(CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")): _USERNAME, vol.Required(CONF_PASSWORD): _PASSWORD}))


class TelegramCallOptionsFlow(OptionsFlow):
    """Chọn người từ danh bạ Telegram làm người nhận: mỗi người được chọn là một thực thể ``notify`` (xem notify.py) — như Zalo Call."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        rt = getattr(self.config_entry, "runtime_data", None)
        if rt is None:
            return self.async_abort(reason="not_loaded")
        try:
            rows = await rt.api.contacts()
        except TelegramCallError:
            return self.async_abort(reason="cannot_connect")
        options = [selector.SelectOptionDict(value=str(r["id"]), label=str(r.get("name") or r.get("username") or r["id"])) for r in rows if r.get("id")]
        valid = {o["value"] for o in options}
        if user_input is not None:
            return self.async_create_entry(data={**self.config_entry.options, "recipients": [v for v in user_input.get("recipients", []) if v in valid]})
        current = [v for v in (self.config_entry.options.get("recipients") or []) if v in valid]
        return self.async_show_form(step_id="init", data_schema=vol.Schema({
            vol.Optional("recipients", default=current): selector.SelectSelector(selector.SelectSelectorConfig(
                options=options, multiple=True, mode=selector.SelectSelectorMode.DROPDOWN, sort=True))}))
