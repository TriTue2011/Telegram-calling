"""Chọn mic và camera mặc định cho cuộc gọi — ngay trên thiết bị (không cần bánh răng). Danh sách lấy từ chính HA:
mic = camera, vệ tinh Assist (loa R1, camera Imou/Dahua) hoặc «URL» (ô Mic URL); camera = mọi camera."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .bridge_core import entity_of_label, label
from .const import CONF_CAMERA, CONF_MIC, CONF_MIC_URL, MIC_URL_LABEL, NONE_LABEL
from .entity import TelegramCallEntity

REFRESH = timedelta(minutes=1)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    rt = entry.runtime_data
    add([DeviceSelect(hass, entry, rt, CONF_MIC, "Mic mặc định", "mdi:microphone", ("camera", "assist_satellite"), (MIC_URL_LABEL,)),
         DeviceSelect(hass, entry, rt, CONF_CAMERA, "Camera mặc định", "mdi:cctv", ("camera",))])


class DeviceSelect(TelegramCallEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass, entry, rt, key, name, icon, domains, extra=()) -> None:
        super().__init__(entry, key)
        self.hass, self._rt, self._key, self._domains, self._extra = hass, rt, key, domains, extra
        self._attr_name, self._attr_icon = name, icon

    def _label_of(self, entity_id: str) -> str:
        st = self.hass.states.get(entity_id)
        return label(entity_id, st.attributes.get("friendly_name") if st else None)

    @property
    def options(self) -> list[str]:
        ids = sorted(e for d in self._domains for e in self.hass.states.async_entity_ids(d))
        opts = [NONE_LABEL, *self._extra, *[self._label_of(e) for e in ids]]
        cur = self.current_option
        if cur and cur not in opts:                      # thực thể đã bị gỡ: vẫn hiện để chọn lại
            opts.insert(1, cur)
        return opts

    @property
    def current_option(self) -> str:
        if self._key == CONF_MIC and self._rt.defaults.get(CONF_MIC_URL) and not self._rt.defaults.get(CONF_MIC):
            return MIC_URL_LABEL
        value = str(self._rt.defaults.get(self._key) or "")
        return self._label_of(entity_of_label(value)) if value else NONE_LABEL

    async def async_select_option(self, option: str) -> None:
        if option == NONE_LABEL:
            patch = {self._key: ""} | ({CONF_MIC_URL: ""} if self._key == CONF_MIC else {})
        elif option == MIC_URL_LABEL:
            patch = {CONF_MIC: ""}
        else:
            patch = {self._key: entity_of_label(option)} | ({CONF_MIC_URL: ""} if self._key == CONF_MIC else {})
        await self._rt.defaults.async_set(**patch)
        await self._rt.bridge.async_push_defaults()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._rt.defaults.add_listener(self.async_write_ha_state))
        self.async_on_remove(async_track_time_interval(self.hass, lambda _now: self.async_write_ha_state(), REFRESH))
