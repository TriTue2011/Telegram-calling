"""Cảm biến «Cuộc gọi»: trạng thái cuộc gọi gần nhất (idle khi chưa có)."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bridge_core import STATES, redact
from .const import SIGNAL_MESSAGE
from .entity import TelegramCallEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([CallStateSensor(entry), LastMessageSensor(entry)])


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


class LastMessageSensor(TelegramCallEntity, SensorEntity):
    """Tin nhắn ĐẾN gần nhất (nội dung rút gọn 255 ký tự; đủ chi tiết ở thuộc tính và sự kiện ``telegram_call_message``)."""
    _attr_translation_key = "last_message"
    _attr_icon = "mdi:message-text"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "last_message")
        self._feed = entry.runtime_data.messages

    @property
    def native_value(self) -> str | None:
        last = self._feed.last
        return ((last.get("text") or last.get("summary") or "")[:255] or None) if last else None

    @property
    def extra_state_attributes(self) -> dict:
        last = self._feed.last
        return {k: last.get(k) for k in ("chat_id", "message_id", "sender", "kind", "file_id", "date")} | {"received": self._feed.received} if last else {}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_MESSAGE.format(self._entry.entry_id), self.async_write_ha_state))
