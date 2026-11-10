"""Cầu nối thiết bị cuộc gọi giữa gateway Telegram và Home Assistant (giai đoạn 1: MIC và CAMERA).

HA PHÂN GIẢI thực thể (camera, vệ tinh Assist của loa R1 / camera Imou, hoặc URL) ra URL luồng rồi đưa cho gateway; gateway tự dùng ffmpeg
đọc và gửi lên cuộc gọi (không bơm tiếng qua HA). Mặc định nằm ở các thực thể select/text của tích hợp (``devices.DeviceDefaults``), mỗi
cuộc gọi ghi đè bằng trường của dịch vụ. KHÔNG ghi URL mic/camera (có thể chứa khoá hay mật khẩu) vào log hay sự kiện.

Chưa có: đưa tiếng BÊN KIA ra loa (cần thêm kiểu phát ``push`` trong gateway để không phải cho HA gửi lệnh shell)."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components import camera as ha_camera
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .api import TelegramCallError, TelegramGatewayApi
from .bridge_core import (PushPolicy, dahua_mic_url, entity_of_label, host_of, localize_url, mic_format_for_url, pick_devices, r1_mic_url,
                          redact, valid_url)
from .const import CONF_HA_URL
from .devices import DeviceDefaults

_LOGGER = logging.getLogger(__name__)


class CallBridge:
    def __init__(self, hass: HomeAssistant, api: TelegramGatewayApi, defaults: DeviceDefaults) -> None:
        self.hass, self.api, self.defaults = hass, api, defaults
        self.policy = PushPolicy()
        self._pushing = False

    def devices_for(self, override: dict[str, Any] | None = None) -> dict[str, str]:
        return pick_devices(self.defaults.data, override)

    def _ha_base(self) -> str:
        base = self.defaults.get(CONF_HA_URL)
        if base:
            return str(base)
        try:
            return get_url(self.hass, allow_external=False, allow_cloud=False)
        except NoURLAvailableError:
            return ""

    async def async_camera_url(self, entity_id: str) -> str:
        try:
            url = await ha_camera.async_get_stream_source(self.hass, entity_id) or ""
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("Camera %s không có luồng: %s", entity_id, redact(str(err)))
            return ""
        return localize_url(url, host_of(self._ha_base()))       # localhost của HA không tới được từ container gateway

    async def _satellite_mic_url(self, entity_id: str) -> str:
        ent = er.async_get(self.hass).async_get(entity_id)
        cfg = self.hass.config_entries.async_get_entry(ent.config_entry_id) if ent and ent.config_entry_id else None
        if cfg is None:
            return ""
        if cfg.domain == "dahua_talk":
            return dahua_mic_url(cfg.options, cfg.data)
        if cfg.domain == "r1_speaker":
            key, base = str(cfg.data.get("intercom_key") or ""), self._ha_base()
            if not key or not base:
                _LOGGER.warning("Loa R1 %s chưa có khoá mic hoặc HA chưa có địa chỉ nội bộ", entity_id)
                return ""
            return r1_mic_url(base, cfg.entry_id, key)
        return ""

    async def async_mic_source(self, devices: dict[str, str]) -> tuple[str, str]:
        """(url, định_dạng) của mic: ``mic_url`` thẳng, hoặc từ thực thể ``mic`` (camera / vệ tinh Assist)."""
        if devices.get("mic_url"):
            url = localize_url(devices["mic_url"], host_of(self._ha_base()))
            return (url, mic_format_for_url(url)) if valid_url(url) else ("", "auto")
        entity_id = entity_of_label(devices.get("mic", ""))
        if not entity_id:
            return "", "auto"
        domain = entity_id.split(".", 1)[0]
        if domain == "camera":
            url = await self.async_camera_url(entity_id)
            return (url, "auto") if valid_url(url) else ("", "auto")
        if domain == "assist_satellite":
            url = await self._satellite_mic_url(entity_id)
            return (url, mic_format_for_url(url)) if valid_url(url) else ("", "auto")
        _LOGGER.warning("Không lấy được mic từ %s (chỉ hỗ trợ camera, assist_satellite hoặc URL)", entity_id)
        return "", "auto"

    async def async_resolve(self, devices: dict[str, str]) -> dict[str, str]:
        mic_url, mic_fmt = await self.async_mic_source(devices)
        cam_id = entity_of_label(devices.get("camera", ""))
        cam_url = await self.async_camera_url(cam_id) if cam_id else ""
        return {"mic_url": mic_url, "mic_format": mic_fmt, "camera_url": cam_url}

    async def async_call_fields(self, override: dict[str, Any] | None) -> dict[str, str]:
        """Trường thiết bị đính kèm ``/api/live/call``: ``mic`` / ``mic_format`` / ``camera`` (bỏ trống = gateway dùng mặc định đã đẩy)."""
        res = await self.async_resolve(self.devices_for(override))
        out: dict[str, str] = {}
        if res["mic_url"]:
            out["mic"], out["mic_format"] = res["mic_url"], res["mic_format"]
        if res["camera_url"]:
            out["camera"] = res["camera_url"]
        return out

    async def async_push_defaults(self) -> None:
        """Đưa mặc định đã phân giải cho gateway (cuộc gọi ĐẾN, và cuộc gọi đi không ghi đè, dùng đúng nguồn này). Lỗi thì KHÔNG bỏ:
        coordinator gọi lại ở lần hỏi gateway thành công kế tiếp (``PushPolicy``)."""
        if self._pushing:
            return
        self._pushing = True
        try:
            res = await self.async_resolve(self.devices_for())
            try:
                await self.api.set_live_settings(res["mic_url"] or "test", res["mic_format"], res["camera_url"] or "test")
            except TelegramCallError as err:
                self.policy.pushed(False)
                _LOGGER.warning("Không đưa được thiết bị mặc định cho gateway (sẽ thử lại): %s", redact(str(err)))
            else:
                self.policy.pushed(True)
        finally:
            self._pushing = False
