"""Thiết bị mặc định của tích hợp (mic, camera, địa chỉ HA): nằm trên THIẾT BỊ (các thực thể select/text), lưu bằng
``Store`` của HA — không phải tuỳ chọn (bánh răng), nên đổi không làm tải lại tích hợp."""
from __future__ import annotations

from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (CONF_CALL_VOLUME, CONF_CAMERA, CONF_HA_URL, CONF_MIC, CONF_MIC_URL, CONF_SPEAKER, DOMAIN, MAX_CALL_VOLUME,
                    STORAGE_VERSION)

DEFAULTS: dict[str, Any] = {CONF_MIC: "", CONF_MIC_URL: "", CONF_CAMERA: "", CONF_HA_URL: "", CONF_SPEAKER: "", CONF_CALL_VOLUME: 0}
_TEXT_KEYS = (CONF_MIC, CONF_MIC_URL, CONF_CAMERA, CONF_HA_URL, CONF_SPEAKER)


class DeviceDefaults:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.devices.{entry_id}")
        self.data: dict[str, Any] = dict(DEFAULTS)
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            self._merge(saved)

    def _merge(self, patch: dict[str, Any]) -> None:
        for key in _TEXT_KEYS:
            if key in patch:
                self.data[key] = str(patch[key] or "").strip()
        if CONF_CALL_VOLUME in patch:
            try:
                self.data[CONF_CALL_VOLUME] = max(0, min(MAX_CALL_VOLUME, int(patch[CONF_CALL_VOLUME])))
            except (TypeError, ValueError):
                pass
        self.data[CONF_HA_URL] = self.data[CONF_HA_URL].rstrip("/")

    def get(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    async def async_set(self, **patch: Any) -> None:
        self._merge(patch)
        await self._store.async_save(self.data)
        for cb in list(self._listeners):
            cb()

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None
