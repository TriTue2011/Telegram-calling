"""Nút «Cúp máy»: kết thúc cuộc gọi đang diễn ra."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import TelegramCallError
from .bridge_core import redact
from .entity import TelegramCallEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([HangupButton(entry)])


class HangupButton(TelegramCallEntity, ButtonEntity):
    _attr_translation_key = "hangup"
    _attr_icon = "mdi:phone-hangup"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "hangup")
        self._rt = entry.runtime_data

    async def async_press(self) -> None:
        live = self._rt.coordinator.live_calls()
        if not live:
            return
        try:
            for c in live:
                await self._rt.api.hangup(str(c["id"]))
        except TelegramCallError as err:
            raise HomeAssistantError(redact(str(err))) from err
        await self._rt.coordinator.async_request_refresh()
