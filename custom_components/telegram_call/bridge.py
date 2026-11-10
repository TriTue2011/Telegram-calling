"""Cầu nối thiết bị cuộc gọi giữa gateway Telegram và Home Assistant: MIC, CAMERA và LOA.

HA PHÂN GIẢI thực thể (camera, vệ tinh Assist của loa R1 / camera Imou, hoặc URL) ra URL luồng rồi đưa cho gateway; gateway tự dùng ffmpeg
đọc và gửi lên cuộc gọi (không bơm tiếng qua HA). Tiếng BÊN KIA ra loa theo kiểu ``push``: HA đưa địa chỉ «cửa bộ đàm» của loa
(``r1_speaker`` / ``dahua_talk``), gateway tự dựng lệnh ffmpeg cố định đẩy tiếng vào đó (HA không gửi lệnh shell). Âm lượng loa được đặt lúc
nghe máy và trả lại khi kết thúc. Mặc định nằm ở các thực thể select/text/number của tích hợp (``devices.DeviceDefaults``), cuộc gọi
ghi đè bằng trường của dịch vụ. KHÔNG ghi URL mic/camera/loa (có thể chứa khoá hay mật khẩu) vào log hay sự kiện.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components import camera as ha_camera
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.network import NoURLAvailableError, get_url

from .api import TelegramCallError, TelegramGatewayApi
from .bridge_core import (PushPolicy, dahua_mic_url, entity_of_label, host_of, intercom_url, localize_url, mic_format_for_url,
                          pick_devices, r1_mic_url, redact, valid_url)
from .const import CONF_CALL_VOLUME, CONF_HA_URL, MAX_CALL_VOLUME
from .devices import DeviceDefaults

_LOGGER = logging.getLogger(__name__)
_PENDING_TTL = 120.0
# Loa làm tròn âm lượng theo nấc của nó (R1 có 15 nấc: đặt 0,77 thì báo 0,80): lệch ít hơn một nấc thì coi là «chưa ai chỉnh».
_VOLUME_TOLERANCE = 0.07
_PUSH_THIEU_TOI_DA = 30       # số lần thử lại (mỗi ~5 s) khi thiết bị đã chọn chưa phân giải được lúc HA khởi động


class _Running:
    def __init__(self) -> None:
        self.volume_restore: tuple[str, float, float] | None = None      # (loa, mức cũ, mức đã đặt)


class CallBridge:
    def __init__(self, hass: HomeAssistant, api: TelegramGatewayApi, defaults: DeviceDefaults) -> None:
        self.hass, self.api, self.defaults = hass, api, defaults
        self.policy = PushPolicy()
        self._pushing = False
        self._push_thieu_lan = 0
        self._pending: dict[str, tuple[float, dict]] = {}
        self._running: dict[str, _Running] = {}

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

    async def async_speaker_sink(self, entity_id: str) -> str:
        """Địa chỉ cửa bộ đàm của loa, hoặc ``""`` nếu loa không hỗ trợ (chỉ ``r1_speaker`` / ``dahua_talk`` có cửa này)."""
        if not entity_id:
            return ""
        ent = er.async_get(self.hass).async_get(entity_id)
        cfg = self.hass.config_entries.async_get_entry(ent.config_entry_id) if ent and ent.config_entry_id else None
        if cfg is None or cfg.domain not in ("r1_speaker", "dahua_talk"):
            _LOGGER.warning("Loa %s không hỗ trợ: chỉ loa R1 hoặc camera Imou/Dahua (cửa bộ đàm) nhận được tiếng bên kia", entity_id)
            return ""
        key, base = str(cfg.data.get("intercom_key") or ""), self._ha_base()
        if not key or not base:
            _LOGGER.warning("Loa %s chưa có khoá bộ đàm hoặc HA chưa có địa chỉ nội bộ", entity_id)
            return ""
        return intercom_url(base, cfg.domain, cfg.entry_id, key)

    async def async_resolve(self, devices: dict[str, str]) -> dict[str, str]:
        mic_url, mic_fmt = await self.async_mic_source(devices)
        cam_id = entity_of_label(devices.get("camera", ""))
        cam_url = await self.async_camera_url(cam_id) if cam_id else ""
        speaker_url = await self.async_speaker_sink(entity_of_label(devices.get("speaker", "")))
        return {"mic_url": mic_url, "mic_format": mic_fmt, "camera_url": cam_url, "speaker_url": speaker_url}

    async def async_call_fields(self, override: dict[str, Any] | None) -> dict[str, str]:
        """Trường thiết bị đính kèm ``/api/live/call``: ``mic`` / ``mic_format`` / ``camera`` / ``speaker`` (bỏ trống = gateway dùng mặc định đã đẩy)."""
        res = await self.async_resolve(self.devices_for(override))
        out: dict[str, str] = {}
        if res["mic_url"]:
            out["mic"], out["mic_format"] = res["mic_url"], res["mic_format"]
        if res["camera_url"]:
            out["camera"] = res["camera_url"]
        if res["speaker_url"]:
            out["speaker"] = res["speaker_url"]
        return out

    async def async_push_defaults(self) -> None:
        """Đưa mặc định đã phân giải cho gateway (cuộc gọi ĐẾN, và cuộc gọi đi không ghi đè, dùng đúng nguồn này). Lỗi thì KHÔNG bỏ:
        coordinator gọi lại ở lần hỏi gateway thành công kế tiếp (``PushPolicy``)."""
        if self._pushing:
            return
        self._pushing = True
        try:
            await self._async_push()
        finally:
            self._pushing = False

    async def _async_push(self) -> None:
        devices = self.devices_for()
        res = await self.async_resolve(devices)
        mic_id = entity_of_label(devices.get("mic", ""))
        cam_id = entity_of_label(devices.get("camera", ""))
        payload = {"mic": res["mic_url"] or "test", "mic_format": res["mic_format"], "camera": res["camera_url"] or "test",
                   "speaker": res["speaker_url"]}
        # Thiết bị ĐÃ CHỌN mà chưa phân giải được (camera chưa nạp xong lúc HA khởi động): KHÔNG gửi «test» để ghi đè giá trị đang đúng
        # của gateway (cùng lớp lỗi đã gặp ở zalo_call, đo 10/11/2026). Bỏ trường đó (gateway giữ giá trị cũ), đánh dấu «chưa xong»
        # để coordinator tự thử lại. «test» chỉ gửi khi người dùng thật sự không chọn thiết bị.
        thieu: list[str] = []
        if cam_id and not res["camera_url"]:
            payload.pop("camera")
            thieu.append("camera")
        if (devices.get("mic_url") or mic_id) and not res["mic_url"]:
            payload.pop("mic")
            payload.pop("mic_format")
            thieu.append("mic")
        try:
            await self.api.set_live_settings(payload)
        except TelegramCallError as err:
            self.policy.pushed(False)
            _LOGGER.warning("Không đưa được thiết bị mặc định cho gateway (sẽ thử lại): %s", redact(str(err)))
            return
        if not thieu:
            self._push_thieu_lan = 0
            self.policy.pushed(True)
            return
        self._push_thieu_lan += 1
        if self._push_thieu_lan >= _PUSH_THIEU_TOI_DA:
            _LOGGER.warning("Không phân giải được %s đã chọn sau %d lần thử; giữ giá trị cũ của gateway. Chọn lại thiết bị hoặc kiểm tra "
                            "thực thể.", ", ".join(thieu), self._push_thieu_lan)
            self.policy.pushed(True)
        else:
            self.policy.pushed(False)

    # -- âm lượng loa theo cuộc gọi -------------------------------------------------------------------------
    def remember(self, call_id: Any, override: dict[str, Any]) -> None:
        """Ghi đè (loa, âm lượng…) của cuộc gọi sắp nối máy. call_id None = cuộc gọi kế tiếp (chưa biết mã lúc gọi dịch vụ)."""
        self._pending[str(call_id) if call_id is not None else "*"] = (time.monotonic(), dict(override))

    def _take(self, call_id: Any) -> dict[str, Any] | None:
        now = time.monotonic()
        self._pending = {k: v for k, v in self._pending.items() if now - v[0] < _PENDING_TTL}
        item = self._pending.pop(str(call_id), None) or self._pending.pop("*", None)
        return item[1] if item else None

    def volume_for(self, override: dict[str, Any] | None) -> int:
        """% âm lượng loa cho cuộc gọi: ghi đè của dịch vụ > mặc định; 0 = không đụng vào loa."""
        raw = (override or {}).get("volume")
        if raw is None:
            raw = self.defaults.get(CONF_CALL_VOLUME)
        try:
            return max(0, min(MAX_CALL_VOLUME, int(raw)))
        except (TypeError, ValueError):
            return 0

    async def start(self, call_id: Any) -> None:
        """Cuộc gọi vừa nghe máy: đặt âm lượng loa (nếu có đặt) và nhớ mức cũ để trả lại khi kết thúc."""
        key = str(call_id)
        if key in self._running:
            return
        override = self._take(call_id)
        devices = self.devices_for(override)
        run = self._running[key] = _Running()
        speaker = entity_of_label(devices.get("speaker", ""))
        if speaker:
            await self._async_set_volume(run, speaker, self.volume_for(override))

    async def stop(self, call_id: Any) -> None:
        run = self._running.pop(str(call_id), None)
        if run is not None:
            await self._async_restore_volume(run)

    async def async_shutdown(self) -> None:
        for key in list(self._running):
            await self.stop(key)

    async def _async_set_volume(self, run: _Running, speaker: str, percent: int) -> None:
        if percent <= 0:
            return
        state = self.hass.states.get(speaker)
        try:
            old = float(state.attributes.get("volume_level")) if state else None
        except (TypeError, ValueError):
            old = None
        target = round(percent / 100, 2)
        try:
            await self.hass.services.async_call("media_player", "volume_set",
                                                {"entity_id": speaker, "volume_level": target}, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.warning("Không đặt được âm lượng %s%% cho loa %s: %s", percent, speaker, err)
            return
        if old is not None:
            run.volume_restore = (speaker, old, target)

    async def _async_restore_volume(self, run: _Running) -> None:
        """Trả âm lượng cũ của loa, trừ khi người dùng đã tự chỉnh trong lúc gọi."""
        restore, run.volume_restore = run.volume_restore, None
        if restore is None:
            return
        speaker, old, target = restore
        state = self.hass.states.get(speaker)
        try:
            now = float(state.attributes.get("volume_level")) if state else None
        except (TypeError, ValueError):
            now = None
        if now is not None and abs(now - target) > _VOLUME_TOLERANCE:
            return
        try:
            await self.hass.services.async_call("media_player", "volume_set",
                                                {"entity_id": speaker, "volume_level": old}, blocking=True)
        except HomeAssistantError as err:
            _LOGGER.debug("Không trả được âm lượng loa %s: %s", speaker, err)
