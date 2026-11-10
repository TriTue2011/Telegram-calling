"""Các ô nhập trên thiết bị: URL mic bất kỳ và địa chỉ HA mà gateway (container) gọi tới được."""
from __future__ import annotations

from homeassistant.components.text import TextEntity, TextMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bridge_core import valid_url
from .const import CONF_HA_URL, CONF_MIC, CONF_MIC_URL
from .entity import TelegramCallEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    add([MicUrlText(entry), HaUrlText(entry)])


class _Text(TelegramCallEntity, TextEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min = 0
    _attr_native_max = 255
    _key = ""

    def __init__(self, entry: ConfigEntry) -> None:
        super().__init__(entry, self._key)
        self._rt = entry.runtime_data

    @property
    def native_value(self) -> str:
        return str(self._rt.defaults.get(self._key) or "")

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._rt.defaults.add_listener(self.async_write_ha_state))


class MicUrlText(_Text):
    _key = CONF_MIC_URL
    _attr_name = "Mic URL"
    _attr_icon = "mdi:microphone-message"
    _attr_mode = TextMode.PASSWORD                       # URL có thể chứa mật khẩu / khoá: che khi hiện

    async def async_set_value(self, value: str) -> None:
        value = value.strip()
        if value and not valid_url(value):
            raise ServiceValidationError("Mic URL phải là rtsp/rtsps/http/https/rtmp, ví dụ rtsp://…")
        await self._rt.defaults.async_set(**{CONF_MIC_URL: value}, **({CONF_MIC: ""} if value else {}))
        await self._rt.bridge.async_push_defaults()


class HaUrlText(_Text):
    _key = CONF_HA_URL
    _attr_name = "Địa chỉ HA mà gateway gọi được"
    _attr_icon = "mdi:home-assistant"

    async def async_set_value(self, value: str) -> None:
        value = value.strip().rstrip("/")
        if value and not value.startswith(("http://", "https://")):
            raise ServiceValidationError("Địa chỉ HA phải bắt đầu bằng http:// hoặc https://")
        await self._rt.defaults.async_set(**{CONF_HA_URL: value})
        await self._rt.bridge.async_push_defaults()      # URL mic của loa R1 dựa vào địa chỉ này
