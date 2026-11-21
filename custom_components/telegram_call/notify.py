"""Người nhận Telegram thành thực thể ``notify``: bạn CHỌN người từ danh bạ Telegram (Cài đặt → Thiết bị & dịch vụ → Telegram Call → Cấu hình → «Người nhận»), mỗi người được
chọn là một thực thể ``notify.<tên>``; người còn lại trong danh bạ vẫn có thực thể nhưng ẨN khỏi bảng điều khiển (hiện trong ô chọn của blueprint).

Mỗi thực thể có thuộc tính ``user_id`` (và ``chat_id`` — hội thoại riêng của Telegram trùng ID người dùng) để truyền thẳng cho ``telegram_call.make_call`` / ``alert_call`` /
``send_message``. Gọi ``notify.send_message`` vào thực thể này cũng gửi tin chữ tới người đó."""
from __future__ import annotations

from datetime import timedelta

from homeassistant.components.notify import NotifyEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .api import TelegramCallError
from .const import DOMAIN
from .entity import device_info

CONF_RECIPIENTS = "recipients"
REFRESH_EVERY = timedelta(hours=1)          # danh bạ đổi (bạn mới) thì thêm thực thể mới sau tối đa chừng này


async def _contact_names(entry: ConfigEntry) -> dict[str, str] | None:
    """{user_id: tên} của TOÀN BỘ danh bạ; None nếu gateway chưa lên (đừng xoá/đổi gì khi không đọc được)."""
    try:
        rows = await entry.runtime_data.api.contacts()
    except TelegramCallError:
        return None
    return {str(r["id"]): str(r.get("name") or r.get("username") or "") for r in rows if r.get("id")}


def _display_names(names: dict[str, str]) -> dict[str, str]:
    """Trùng tên thì thêm 4 số cuối ID để phân biệt ngay trong ô chọn."""
    count: dict[str, int] = {}
    for n in names.values():
        count[n] = count.get(n, 0) + 1
    return {u: ("%s (…%s)" % (n, u[-4:]) if n and count[n] > 1 else (n or u)) for u, n in names.items()}


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, add: AddEntitiesCallback) -> None:
    chosen = [str(v) for v in (entry.options.get(CONF_RECIPIENTS) or [])]
    added: set[str] = set()

    def uid_of(user_id: str) -> str:
        return "%s_to_%s" % (entry.entry_id, user_id)

    def build(user_ids: list[str], shown: dict[str, str]) -> list["TelegramRecipient"]:
        out = []
        for u in user_ids:
            if uid_of(u) not in added:
                added.add(uid_of(u))
                out.append(TelegramRecipient(entry, u, shown.get(u) or u, visible=u in chosen))
        return out

    names = await _contact_names(entry)
    shown = _display_names(names or {})
    everyone = list(dict.fromkeys(chosen + list((names or {}).keys())))
    registry = er.async_get(hass)
    if names is not None:                                         # chỉ dọn sổ đăng ký khi đã đọc được danh bạ (gateway tắt không làm mất thực thể)
        keep = {uid_of(u) for u in everyone}
        for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
            if ent.domain == "notify" and (ent.unique_id or "").startswith("%s_to_" % entry.entry_id) and ent.unique_id not in keep:
                registry.async_remove(ent.entity_id)
    for u in chosen:                                              # vừa được chọn: bỏ ẩn
        eid = registry.async_get_entity_id("notify", DOMAIN, uid_of(u))
        ent = registry.async_get(eid) if eid else None
        if ent is not None and ent.hidden_by == er.RegistryEntryHider.INTEGRATION:
            registry.async_update_entity(eid, hidden_by=None)
    add(build(everyone, shown))

    async def refresh(_now) -> None:
        fresh = await _contact_names(entry)
        if fresh:
            new = build(list(fresh.keys()), _display_names(fresh))
            if new:
                add(new)

    entry.async_on_unload(async_track_time_interval(hass, refresh, REFRESH_EVERY))


class TelegramRecipient(NotifyEntity):
    """Một người Telegram để gửi tin / gọi."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:account-box-outline"

    def __init__(self, entry: ConfigEntry, user_id: str, name: str, visible: bool = True) -> None:
        self._entry = entry
        self._attr_entity_registry_visible_default = visible
        self._uid = str(user_id)
        self._attr_unique_id = "%s_to_%s" % (entry.entry_id, self._uid)
        self._attr_name = name

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(self._entry)

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"user_id": self._uid, "chat_id": self._uid, "type": "user"}

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        text = "%s\n%s" % (title, message) if title else message
        await self.hass.services.async_call(DOMAIN, "send_message", {"peer": self._uid, "message": text}, blocking=True)
