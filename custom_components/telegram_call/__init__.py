"""Telegram Call — gọi thoại/video và nhắn tin Telegram qua telegram-gateway (cùng kho ``Calling``).

Tích hợp này không tự nói chuyện với Telegram: nó gọi sang gateway (container ``telegram-gateway``, TDLib + NTgCalls), giống cách
``zalo_call`` gọi app Zalo PC. HA hỏi ``/api/live/calls`` (dày hơn khi có cuộc gọi) rồi bắn sự kiện ``telegram_call_call_state`` với
CÙNG tên trạng thái như ``zalo_call_call_state`` (incoming / ringing / answered / ended / …)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TelegramCallAuthError, TelegramCallError, TelegramGatewayApi
from .bridge import CallBridge
from .bridge_core import derive_state, redact
from .const import CONF_ACCOUNT, CONF_TOKEN, CONF_URL, DOMAIN, EVENT_STATE, POLL_BUSY_S, POLL_IDLE_S
from .devices import DeviceDefaults
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "binary_sensor", "select", "text", "button"]
TERMINAL = ("ended", "not_answered", "missed", "error")


@dataclass
class TelegramCallData:
    api: TelegramGatewayApi
    coordinator: "TelegramCallCoordinator"
    bridge: CallBridge
    defaults: DeviceDefaults


class TelegramCallCoordinator(DataUpdateCoordinator[dict]):
    """Danh sách cuộc gọi của gateway: 5 giây một lần khi rảnh, 2 giây khi có cuộc gọi. Mỗi lần đổi trạng thái bắn một sự kiện."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: TelegramGatewayApi) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(seconds=POLL_IDLE_S))
        self.api = api
        self.bridge: CallBridge | None = None
        self._seen: dict[str, str] = {}            # id cuộc gọi → trạng thái đã báo
        self._was_active: set[str] = set()
        self.last_state: str = "idle"
        self.last_call: dict = {}

    @staticmethod
    def is_live(call: dict) -> bool:
        return call.get("state") != "ended"

    def live_calls(self) -> list[dict]:
        return [c for c in (self.data or {}).get("calls", []) if self.is_live(c)]

    async def _async_update_data(self) -> dict:
        try:
            calls = await self.api.live_calls()
        except TelegramCallAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TelegramCallError as err:
            if self.bridge is not None:
                self.bridge.policy.poll(False)
            raise UpdateFailed(str(err)) from err
        if self.bridge is not None and self.bridge.policy.poll(True):
            self.hass.async_create_task(self.bridge.async_push_defaults())
        self._on_calls(calls)
        busy = any(self.is_live(c) for c in calls)
        self.update_interval = timedelta(seconds=POLL_BUSY_S if busy else POLL_IDLE_S)
        return {"calls": calls}

    def _on_calls(self, calls: list[dict]) -> None:
        for c in calls:
            cid = str(c.get("id"))
            if c.get("state") == "active":
                self._was_active.add(cid)
            state = derive_state(c, cid in self._was_active)
            if state is None or self._seen.get(cid) == state:
                continue
            self._seen[cid] = state
            self.last_state, self.last_call = state, c
            # Không đưa URL mic/camera (có thể chứa khoá) lên bus: chỉ thông tin cuộc gọi.
            self.hass.bus.async_fire(EVENT_STATE, {
                "state": state, "call_id": cid, "user_id": c.get("user_id"), "outgoing": bool(c.get("outgoing")),
                "video": bool(c.get("video")), "seconds": c.get("seconds"), "error": redact(str(c.get("error") or ""))})
        ids = {str(c.get("id")) for c in calls}
        for old in [k for k in self._seen if k not in ids]:           # gateway chỉ giữ 10 cuộc gần nhất
            self._seen.pop(old, None)
            self._was_active.discard(old)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = TelegramGatewayApi(async_get_clientsession(hass), entry.data[CONF_URL], entry.data[CONF_TOKEN], entry.data.get(CONF_ACCOUNT, ""))
    try:
        await api.health()
        await api.accounts()                           # kiểm token (health không cần token)
    except TelegramCallAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except TelegramCallError as err:
        raise ConfigEntryNotReady(str(err)) from err
    coordinator = TelegramCallCoordinator(hass, entry, api)
    defaults = DeviceDefaults(hass, entry.entry_id)
    await defaults.async_load()
    bridge = CallBridge(hass, api, defaults)
    coordinator.bridge = bridge
    entry.runtime_data = TelegramCallData(api, coordinator, bridge, defaults)
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.async_create_task(bridge.async_push_defaults())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
