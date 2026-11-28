"""Công tắc trên thiết bị: tự nhấn mạnh chữ khi gửi tin nhắn."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import CONF_TEXT_AUTO_EMPHASIS
from .entity import TelegramCallEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([AutoEmphasisSwitch(entry, entry.runtime_data)])


class AutoEmphasisSwitch(TelegramCallEntity, RestoreEntity, SwitchEntity):
    """Bật/tắt tự động nhấn mạnh từ khoá khi gửi tin nhắn."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Chữ: tự nhấn mạnh"
    _attr_icon = "mdi:format-bold"

    def __init__(self, entry: ConfigEntry, rt) -> None:
        super().__init__(entry, CONF_TEXT_AUTO_EMPHASIS)
        self._rt = rt

    @property
    def is_on(self) -> bool:
        return bool(self._rt.defaults.get(CONF_TEXT_AUTO_EMPHASIS))

    async def async_turn_on(self, **kwargs) -> None:
        await self._rt.defaults.async_set(**{CONF_TEXT_AUTO_EMPHASIS: True})

    async def async_turn_off(self, **kwargs) -> None:
        await self._rt.defaults.async_set(**{CONF_TEXT_AUTO_EMPHASIS: False})

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if CONF_TEXT_AUTO_EMPHASIS not in self._rt.defaults.stored_keys:
            last = await self.async_get_last_state()
            if last is not None and last.state in ("on", "off"):
                await self._rt.defaults.async_set(**{CONF_TEXT_AUTO_EMPHASIS: last.state == "on"})
        self.async_on_remove(self._rt.defaults.add_listener(self.async_write_ha_state))
