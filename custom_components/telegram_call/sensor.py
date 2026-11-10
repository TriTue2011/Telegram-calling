"""Cảm biến «Cuộc gọi»: trạng thái cuộc gọi gần nhất (idle khi chưa có)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bridge_core import STATES, redact
from .entity import TelegramCallEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([CallStateSensor(entry)])


class CallStateSensor(TelegramCallEntity, SensorEntity):
    _attr_translation_key = "call_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", *STATES]
    _attr_icon = "mdi:phone"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "call_state")
        self._coordinator = entry.runtime_data.coordinator

    @property
    def native_value(self) -> str:
        return self._coordinator.last_state

    @property
    def extra_state_attributes(self) -> dict:
        c = self._coordinator.last_call
        return {"call_id": c.get("id"), "user_id": c.get("user_id"), "outgoing": c.get("outgoing"), "video": c.get("video"),
                "seconds": c.get("seconds"), "error": redact(str(c.get("error") or "")) or None} if c else {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(callback(self.async_write_ha_state)))
