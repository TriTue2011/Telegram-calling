"""Nút điều khiển cuộc gọi đang diễn ra: cúp máy, người nhà nói trực tiếp, tắt mic, tắt hình (như Zalo Call)."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import call_rules
from .api import TelegramCallError
from .bridge_core import redact
from .entity import TelegramCallEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([HangupButton(entry), CallButton(entry, "take_over", "mdi:phone-in-talk"), CallButton(entry, "stop_mic", "mdi:microphone-off"),
         CallButton(entry, "stop_video", "mdi:video-off")])


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


class CallButton(TelegramCallEntity, ButtonEntity):
    """Người nhà nói trực tiếp (nối mic/loa/camera nhà), tắt mic, tắt hình — cho cuộc gọi đang nói chuyện."""

    def __init__(self, entry: ConfigEntry, key: str, icon: str) -> None:
        super().__init__(entry, "btn_" + key)
        self._attr_translation_key = key
        self._attr_icon = icon
        self._key = key
        self._rt = entry.runtime_data

    async def async_press(self) -> None:
        try:
            if self._key == "take_over":
                await call_rules.async_take_over(self.hass, self._rt)
            else:
                await call_rules.async_mute(self._rt, None, "mic" if self._key == "stop_mic" else "video")
        except TelegramCallError as err:
            raise HomeAssistantError(redact(str(err))) from err
