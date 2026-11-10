"""«Đang gọi»: bật khi gateway có cuộc gọi chưa kết thúc; «Gateway»: nối được gateway."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import TelegramCallEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([InCallSensor(entry), GatewaySensor(entry)])


class _Base(TelegramCallEntity, BinarySensorEntity):
    def __init__(self, entry: ConfigEntry, key: str) -> None:
        super().__init__(entry, key)
        self._coordinator = entry.runtime_data.coordinator

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(callback(self.async_write_ha_state)))


class InCallSensor(_Base):
    _attr_translation_key = "in_call"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_icon = "mdi:phone-in-talk"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "in_call")

    @property
    def is_on(self) -> bool:
        return bool(self._coordinator.live_calls())


class GatewaySensor(_Base):
    _attr_translation_key = "gateway"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "gateway")

    @property
    def is_on(self) -> bool:
        return self._coordinator.last_update_success
