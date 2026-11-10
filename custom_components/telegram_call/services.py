"""Dịch vụ của Telegram Call: make_call, answer_call, hangup_call, send_message."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import TelegramCallError
from .bridge_core import redact
from .const import DOMAIN

_ENTRY = {vol.Optional("config_entry_id"): cv.string}
MAKE_CALL = vol.Schema({
    **_ENTRY,
    vol.Required("user_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("video", default=False): cv.boolean,
    vol.Optional("mic"): cv.string, vol.Optional("mic_url"): cv.string, vol.Optional("camera"): cv.string,
})
CALL_ID = vol.Schema({**_ENTRY, vol.Optional("call_id"): cv.string})
SEND = vol.Schema({**_ENTRY, vol.Required("chat_id"): vol.All(vol.Coerce(int), vol.Range(min=1)), vol.Required("text"): cv.string})


def _entry(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    want = call.data.get("config_entry_id")
    if want:
        entries = [e for e in entries if e.entry_id == want]
    if len(entries) != 1:
        raise ServiceValidationError("Chưa có (hoặc có nhiều) tài khoản Telegram Call: chỉ rõ config_entry_id" if not want
                                     else f"Không có mục cấu hình {want}")
    return entries[0]


def _public(info: dict) -> dict:
    """Thông tin cuộc gọi trả cho người dùng/automation: không có nhật ký sự kiện (có thể kèm lỗi dài/URL)."""
    return {k: (redact(str(v)) if k == "error" else v) for k, v in info.items()
            if k in ("id", "user_id", "outgoing", "video", "state", "emojis", "error", "seconds")}


def async_register_services(hass: HomeAssistant) -> None:
    async def run(call: ServiceCall, fn):
        try:
            return await fn(_entry(hass, call).runtime_data)
        except TelegramCallError as err:
            raise HomeAssistantError(redact(str(err))) from err

    async def make_call(call: ServiceCall) -> ServiceResponse:
        override = {k: call.data[k] for k in ("mic", "mic_url", "camera") if k in call.data}

        async def go(rt):
            fields = await rt.bridge.async_call_fields(override)
            return _public(await rt.api.live_call(call.data["user_id"], call.data["video"], **{
                "mic": fields.get("mic"), "mic_format": fields.get("mic_format"), "camera": fields.get("camera")}))
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def _pick(rt, call: ServiceCall, incoming: bool) -> str:
        """call_id đã cho, hoặc cuộc gọi DUY NHẤT phù hợp: (nghe) cuộc gọi đến đang đổ chuông, (cúp) cuộc gọi đang diễn ra."""
        if call.data.get("call_id"):
            return call.data["call_id"]
        live = rt.coordinator.live_calls()
        if incoming:
            live = [c for c in live if not c.get("outgoing") and c.get("state") == "ringing"]
        if len(live) != 1:
            raise ServiceValidationError("Cần call_id: " + ("không có đúng một cuộc gọi đến đang đổ chuông" if incoming
                                                            else "không có đúng một cuộc gọi đang diễn ra"))
        return str(live[0]["id"])

    async def answer_call(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return _public(await rt.api.accept(await _pick(rt, call, True)))
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def hangup_call(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return _public(await rt.api.hangup(await _pick(rt, call, False)))
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def send_message(call: ServiceCall) -> None:
        async def go(rt):
            await rt.api.send_message(call.data["chat_id"], call.data["text"])
        await run(call, go)

    for name, fn, schema, resp in (("make_call", make_call, MAKE_CALL, SupportsResponse.OPTIONAL),
                                   ("answer_call", answer_call, CALL_ID, SupportsResponse.OPTIONAL),
                                   ("hangup_call", hangup_call, CALL_ID, SupportsResponse.OPTIONAL),
                                   ("send_message", send_message, SEND, SupportsResponse.NONE)):
        hass.services.async_register(DOMAIN, name, fn, schema=schema, supports_response=resp)


async def _refresh(hass: HomeAssistant, call: ServiceCall) -> None:
    try:
        await _entry(hass, call).runtime_data.coordinator.async_request_refresh()
    except ServiceValidationError:
        pass
