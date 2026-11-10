"""Âm lượng loa trong lúc gọi: đặt khi nghe máy, trả mức cũ khi cuộc gọi kết thúc."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CALL_VOLUME, MAX_CALL_VOLUME
from .entity import TelegramCallEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([CallVolume(entry, entry.runtime_data)])


class CallVolume(TelegramCallEntity, NumberEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_name = "Âm lượng cuộc gọi"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_max_value = MAX_CALL_VOLUME
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, entry: ConfigEntry, rt) -> None:
        super().__init__(entry, CONF_CALL_VOLUME)
        self._rt = rt

    @property
    def native_value(self) -> float:
        return float(self._rt.defaults.get(CONF_CALL_VOLUME) or 0)

    async def async_set_native_value(self, value: float) -> None:
        # Chỉ HA dùng (bridge.start/stop đặt rồi trả âm lượng loa), không cần báo cho gateway.
        await self._rt.defaults.async_set(**{CONF_CALL_VOLUME: int(value)})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._rt.defaults.add_listener(self.async_write_ha_state))
