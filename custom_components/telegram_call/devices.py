"""Thiết bị mặc định của tích hợp (mic, camera, địa chỉ HA): nằm trên THIẾT BỊ (các thực thể select/text), lưu bằng
``Store`` của HA — không phải tuỳ chọn (bánh răng), nên đổi không làm tải lại tích hợp."""
from __future__ import annotations

from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (CONF_CALL_VOLUME, CONF_CAMERA, CONF_ECHO_GUARD, CONF_HA_URL, CONF_MIC, CONF_MIC_URL, CONF_SPEAKER,
                    CONF_TEXT_AUTO_EMPHASIS, CONF_TEXT_FORMAT, DOMAIN, MAX_CALL_VOLUME, STORAGE_VERSION, TEXT_FORMATS)
from .echo_policy import ECHO_MODES
from .rules import RULES_KEY

DEFAULTS: dict[str, Any] = {CONF_MIC: "", CONF_MIC_URL: "", CONF_CAMERA: "", CONF_HA_URL: "", CONF_SPEAKER: "",
                            CONF_ECHO_GUARD: "auto", CONF_CALL_VOLUME: 0, CONF_TEXT_FORMAT: "markdown",
                            CONF_TEXT_AUTO_EMPHASIS: True, RULES_KEY: {}}
_TEXT_KEYS = (CONF_MIC, CONF_MIC_URL, CONF_CAMERA, CONF_HA_URL, CONF_SPEAKER)


class DeviceDefaults:
    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store = Store(hass, STORAGE_VERSION, f"{DOMAIN}.devices.{entry_id}")
        self.data: dict[str, Any] = dict(DEFAULTS)
        self.stored_keys: set[str] = set()
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        saved = await self._store.async_load()
        if isinstance(saved, dict):
            self.stored_keys.update(saved.keys())
            self._merge(saved)

    def _merge(self, patch: dict[str, Any]) -> None:
        for key in _TEXT_KEYS:
            if key in patch:
                self.data[key] = str(patch[key] or "").strip()
        if CONF_ECHO_GUARD in patch and patch[CONF_ECHO_GUARD] in ECHO_MODES:
            self.data[CONF_ECHO_GUARD] = str(patch[CONF_ECHO_GUARD])
        if CONF_TEXT_FORMAT in patch and patch[CONF_TEXT_FORMAT] in TEXT_FORMATS:
            self.data[CONF_TEXT_FORMAT] = str(patch[CONF_TEXT_FORMAT])
        if CONF_TEXT_AUTO_EMPHASIS in patch and patch[CONF_TEXT_AUTO_EMPHASIS] is not None:
            val = patch[CONF_TEXT_AUTO_EMPHASIS]
            if isinstance(val, bool):
                self.data[CONF_TEXT_AUTO_EMPHASIS] = val
            elif isinstance(val, str):
                self.data[CONF_TEXT_AUTO_EMPHASIS] = val.strip().lower() not in ("false", "0", "off", "no", "")
            else:
                self.data[CONF_TEXT_AUTO_EMPHASIS] = bool(val)
        if CONF_CALL_VOLUME in patch:
            try:
                self.data[CONF_CALL_VOLUME] = max(0, min(MAX_CALL_VOLUME, int(patch[CONF_CALL_VOLUME])))
            except (TypeError, ValueError):
                pass
        if isinstance(patch.get(RULES_KEY), dict):               # kho quy tắc (set_rule): thay nguyên khối, không trộn từng khoá
            self.data[RULES_KEY] = patch[RULES_KEY]
        self.data[CONF_HA_URL] = self.data[CONF_HA_URL].rstrip("/")

    def get(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    async def async_set(self, **patch: Any) -> None:
        self.stored_keys.update(patch.keys())
        self._merge(patch)
        await self._store.async_save(self.data)
        for cb in list(self._listeners):
            cb()

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb) if cb in self._listeners else None
