"""«Đang gọi»: bật khi gateway có cuộc gọi chưa kết thúc; «Gateway»: nối được gateway."""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import TelegramCallEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([InCallSensor(entry), GatewaySensor(entry), LoggedInSensor(entry)])


class _Base(TelegramCallEntity, BinarySensorEntity):
    def __init__(self, entry: ConfigEntry, key: str) -> None:
        super().__init__(entry, key)
        self._coordinator = entry.runtime_data.coordinator

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._coordinator.async_add_listener(self.async_write_ha_state))


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


class LoggedInSensor(TelegramCallEntity, BinarySensorEntity):
    """«Đã đăng nhập Telegram»: tài khoản trên gateway đang online (đăng nhập qua trang web của gateway, không qua HA)."""
    _attr_translation_key = "logged_in"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:telegram"

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, "logged_in")
        self._account = entry.runtime_data.account

    @property
    def is_on(self) -> bool | None:
        data = self._account.data
        return None if not data else data.get("status") in ("online", "mock")

    @property
    def extra_state_attributes(self) -> dict:
        return {"status": (self._account.data or {}).get("status"), "display_name": (self._account.data or {}).get("display_name")}

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._account.async_add_listener(self.async_write_ha_state))
