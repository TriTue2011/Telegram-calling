"""Gốc chung của các thực thể Telegram Call: gắn vào MỘT thiết bị trong HA."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .accounts import device_name
from .const import CONF_ACCOUNT, DOMAIN


def device_info(entry: ConfigEntry) -> DeviceInfo:
    data = entry.runtime_data.account.data or {}
    return DeviceInfo(identifiers={(DOMAIN, entry.entry_id)}, name=device_name(data.get("phone") or entry.data.get(CONF_ACCOUNT, ""), data.get("display_name", "")),
                      manufacturer="TriTue2011", model="telegram-gateway")


class TelegramCallEntity(Entity):
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, key: str) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(self._entry)
