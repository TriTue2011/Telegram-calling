"""Telegram Call — gọi thoại/video và nhắn tin Telegram qua telegram-gateway (cùng kho ``Calling``).

Tích hợp này không tự nói chuyện với Telegram: nó gọi sang gateway (container ``telegram-gateway``, TDLib + NTgCalls), giống cách
``zalo_call`` gọi app Zalo PC. HA hỏi ``/api/live/calls`` (dày hơn khi có cuộc gọi) rồi bắn sự kiện ``telegram_call_call_state`` với
CÙNG tên trạng thái như ``zalo_call_call_state`` (incoming / ringing / answered / ended / …)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TelegramCallAuthError, TelegramCallError, TelegramGatewayApi
from .bridge import CallBridge
from . import call_rules
from .bridge_core import derive_state, redact
from .call_rules import CallVoice
from .contacts import ContactBook
from .const import CONF_ACCOUNT, CONF_PASSWORD, CONF_URL, CONF_USERNAME, DOMAIN, EVENT_MESSAGE, EVENT_STATE, POLL_BUSY_S, POLL_IDLE_S
from .devices import DeviceDefaults
from .messages import MessageFeed
from .rules import get_rules
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["sensor", "binary_sensor", "select", "switch", "text", "number", "button", "notify"]
ACCOUNT_POLL_S = 60
TERMINAL = ("ended", "not_answered", "missed", "error")


@dataclass
class TelegramCallData:
    api: TelegramGatewayApi
    coordinator: "TelegramCallCoordinator"
    bridge: CallBridge
    defaults: DeviceDefaults
    messages: MessageFeed
    voice: CallVoice
    contacts: ContactBook
    account: "AccountCoordinator"


class TelegramCallCoordinator(DataUpdateCoordinator[dict]):
    """Danh sách cuộc gọi của gateway: 5 giây một lần khi rảnh, 2 giây khi có cuộc gọi. Mỗi lần đổi trạng thái bắn một sự kiện."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: TelegramGatewayApi) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=timedelta(seconds=POLL_IDLE_S))
        self.api = api
        self.bridge: CallBridge | None = None
        self._seen: dict[str, str] = {}            # id cuộc gọi → trạng thái đã báo
        self._was_active: set[str] = set()
        self._peer_video: dict[str, bool] = {}     # id cuộc gọi → bên kia đang bật camera (phát hiện «xin chuyển sang video»)
        self.last_state: str = "idle"
        self.last_call: dict = {}
        self.contacts: ContactBook | None = None
        self.defaults: DeviceDefaults | None = None

    @staticmethod
    def is_live(call: dict) -> bool:
        return call.get("state") != "ended"

    def live_calls(self) -> list[dict]:
        return [c for c in (self.data or {}).get("calls", []) if self.is_live(c)]

    def current_state(self) -> tuple[str, dict]:
        """(trạng thái, cuộc gọi) ĐANG diễn ra, hoặc ("idle", {}) khi không có. Cảm biến dùng cái này để automation hỏi «đang có cuộc gọi không?»
        (trước đây cảm biến giữ mãi trạng thái cuối như «missed» cho tới cuộc gọi sau, nên không phân biệt được rảnh với vừa nhỡ)."""
        live = self.live_calls()
        if not live:
            return "idle", {}
        c = live[0]
        return derive_state(c, str(c.get("id")) in self._was_active) or "idle", c

    async def _async_update_data(self) -> dict:
        try:
            calls = await self.api.live_calls()
        except TelegramCallAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TelegramCallError as err:
            if self.bridge is not None:
                self.bridge.policy.poll(False)
            raise UpdateFailed(str(err)) from err
        if self.bridge is not None and self.bridge.policy.poll(True):
            self.hass.async_create_task(self.bridge.async_push_defaults())
        events = await self._on_calls(calls)
        busy = any(self.is_live(c) for c in calls)
        self.update_interval = timedelta(seconds=POLL_BUSY_S if busy else POLL_IDLE_S)
        # Bắn sự kiện SAU khi cảm biến đã có trạng thái mới (dữ liệu được đặt và các thực thể được ghi ngay sau khi hàm này trả về, cùng một lượt của vòng lặp):
        # automation bắt sự kiện rồi đọc cảm biến (blueprint tự nghe máy, nút bấm) phải thấy trạng thái MỚI, không phải trạng thái cũ.
        if events:
            self.hass.loop.call_soon(self._fire, events)
        return {"calls": calls}

    def _fire(self, events: list[dict]) -> None:
        rt = getattr(self.config_entry, "runtime_data", None)
        who = {"config_entry_id": self.config_entry.entry_id, "account": self.config_entry.data.get(CONF_ACCOUNT, "")}
        for event in events:
            self.hass.bus.async_fire(EVENT_STATE, {**event, **who})      # gateway nhiều tài khoản: automation biết cuộc gọi thuộc tài khoản nào
        for event in events:                                      # SAU khi sự kiện đã bắn: automation kịp gửi quy tắc (apply_call_rule) trong thời gian chờ chốt
            if rt is not None:
                call_rules.on_call_state(self.hass, rt, event["state"], event["call_id"], TERMINAL, event)

    async def _on_calls(self, calls: list[dict]) -> list[dict]:
        events: list[dict] = []
        for c in calls:
            cid = str(c.get("id"))
            if c.get("state") == "active":
                self._was_active.add(cid)
            state = derive_state(c, cid in self._was_active)
            if state is None or self._seen.get(cid) == state:
                continue
            self._seen[cid] = state
            self.last_state, self.last_call = state, c
            # Nghe máy: đặt âm lượng loa (nếu có đặt); kết thúc: trả lại mức cũ.
            if self.bridge is not None:
                if state == "answered":
                    self.hass.async_create_task(self.bridge.start(cid))
                elif state in TERMINAL:
                    self.hass.async_create_task(self.bridge.stop(cid))
            # Không đưa URL mic/camera (có thể chứa khoá) lên bus: chỉ thông tin cuộc gọi. Người gọi: quen/lạ, tên đã lưu, thẻ (cho quy tắc theo phạm vi, như zalo_call).
            status, name = await self.contacts.async_lookup(c.get("user_id")) if self.contacts is not None else ("unknown", "")
            tags = ((get_rules(self.defaults).get("contact_meta") or {}).get(str(c.get("user_id"))) or {}).get("tags", []) if self.defaults is not None else []
            events.append({
                "state": state, "call_id": cid, "user_id": c.get("user_id"), "outgoing": bool(c.get("outgoing")),
                "video": bool(c.get("video")), "call_type": "video" if c.get("video") else "voice", "seconds": c.get("seconds"),
                "contact_status": status, "name": name, "tags": list(tags), "error": redact(str(c.get("error") or ""))})
        for c in calls:                                   # bên kia vừa BẬT camera giữa cuộc thoại đang nói = xin chuyển sang video
            cid, on = str(c.get("id")), bool(c.get("peer_video"))
            was = self._peer_video.get(cid, False)
            self._peer_video[cid] = on
            if on and not was and c.get("state") == "active" and not c.get("video"):
                status, name = await self.contacts.async_lookup(c.get("user_id")) if self.contacts is not None else ("unknown", "")
                events.append({"state": "video_request", "call_id": cid, "user_id": c.get("user_id"), "outgoing": bool(c.get("outgoing")), "video": False,
                               "call_type": "voice", "seconds": c.get("seconds"), "contact_status": status, "name": name, "tags": [], "error": ""})
        ids = {str(c.get("id")) for c in calls}
        for old in [k for k in self._peer_video if k not in ids]:
            self._peer_video.pop(old, None)
        for old in [k for k in self._seen if k not in ids]:           # gateway chỉ giữ một số cuộc gần nhất
            if self._seen.get(old) not in TERMINAL:                   # cuộc biến mất khi chưa thấy kết thúc (gateway khởi động lại giữa cuộc gọi): HA không bao giờ nhận «ended»
                self._forget_vanished(old)
            self._seen.pop(old, None)
            self._was_active.discard(old)
        return events

    def _forget_vanished(self, cid: str) -> None:
        """Dọn mọi thứ gắn theo call_id (hẹn giờ, tác vụ, quy tắc đã chốt, âm lượng loa đã hạ) cho cuộc gọi biến mất mà không có trạng thái kết thúc.
        Không bắn sự kiện lên bus: automation không được báo một cuộc «kết thúc» mà HA chưa từng thấy kết thúc."""
        rt = getattr(self.config_entry, "runtime_data", None)
        if self.bridge is not None:
            self.hass.async_create_task(self.bridge.stop(cid))
        if rt is not None:
            call_rules.on_call_state(self.hass, rt, "ended", cid, TERMINAL)


class AccountCoordinator(DataUpdateCoordinator[dict]):
    """Trạng thái đăng nhập của tài khoản Telegram trên gateway (60 giây một lần): online / offline / chờ đăng nhập…"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: TelegramGatewayApi) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_account", config_entry=entry, update_interval=timedelta(seconds=ACCOUNT_POLL_S))
        self.api = api
        self._phone = entry.data.get(CONF_ACCOUNT, "")

    async def _async_update_data(self) -> dict:
        try:
            accounts = await self.api.accounts()
        except TelegramCallAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except TelegramCallError as err:
            raise UpdateFailed(str(err)) from err
        mine = next((a for a in accounts if a.get("phone") == self._phone), None) or (accounts[0] if len(accounts) == 1 and not self._phone else None)
        if mine is None:
            return {"status": "missing", "display_name": ""}
        return {"status": str(mine.get("status") or "unknown"), "display_name": str(mine.get("display_name") or "")}


async def _async_reload_on_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    legacy = entry.data.get("token", "") if not (entry.data.get(CONF_USERNAME) and entry.data.get(CONF_PASSWORD)) else ""
    if not legacy and not (entry.data.get(CONF_USERNAME) and entry.data.get(CONF_PASSWORD)):
        raise ConfigEntryAuthFailed("Cần tên đăng nhập và mật khẩu web của gateway")
    api = TelegramGatewayApi(async_get_clientsession(hass), entry.data[CONF_URL], entry.data.get(CONF_USERNAME, ""), entry.data.get(CONF_PASSWORD, ""),
                             entry.data.get(CONF_ACCOUNT, ""), legacy_token=legacy)
    if api.legacy:
        # Mục cũ chỉ có token: vẫn chạy (không mất tự nghe máy). Vào Cấu hình lại, nhập tên đăng nhập + mật khẩu web để bỏ token.
        _LOGGER.warning("Telegram Call %s còn dùng token cũ — hãy Cấu hình lại bằng tên đăng nhập và mật khẩu web của gateway", entry.title)
    try:
        await api.health()
        await api.login()
        await api.accounts()                           # kiểm phiên đăng nhập (health không cần đăng nhập)
    except TelegramCallAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except TelegramCallError as err:
        raise ConfigEntryNotReady(str(err)) from err
    coordinator = TelegramCallCoordinator(hass, entry, api)
    defaults = DeviceDefaults(hass, entry.entry_id)
    await defaults.async_load()
    bridge = CallBridge(hass, api, defaults)
    coordinator.bridge = bridge
    coordinator.defaults, coordinator.contacts = defaults, ContactBook(api)
    account = AccountCoordinator(hass, entry, api)
    messages = MessageFeed(hass, api, entry.entry_id)
    entry.runtime_data = TelegramCallData(api, coordinator, bridge, defaults, messages, CallVoice(), coordinator.contacts, account)

    @callback
    def _on_message(event) -> None:
        hass.async_create_task(call_rules.async_reply_to_message(hass, entry.runtime_data, event.data))
    entry.async_on_unload(hass.bus.async_listen(EVENT_MESSAGE, _on_message))

    await coordinator.async_config_entry_first_refresh()
    await account.async_refresh()                                  # lỗi tạm thời không chặn nạp tích hợp: cảm biến báo «không rõ» tới lần hỏi sau
    hass.async_create_task(coordinator.contacts.async_lookup(0))   # làm nóng danh bạ: cuộc gọi đến đầu tiên không phải chờ tải
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    hass.async_create_task(bridge.async_push_defaults())
    entry.async_create_background_task(hass, messages.run(), f"{DOMAIN}_messages")       # huỷ tự động khi gỡ mục cấu hình
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        entry.runtime_data.voice.cancel_all()
        await entry.runtime_data.bridge.async_shutdown()          # trả âm lượng loa của cuộc gọi đang mở
    return ok
