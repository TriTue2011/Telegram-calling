"""Nghe tin nhắn đến từ gateway (``/api/feed``, long-poll) và bắn sự kiện HA.

Một vòng nền cho mỗi tài khoản: gateway giữ yêu cầu tối đa 20 giây rồi trả (3 yêu cầu/phút, rất xa trần 120/phút của gateway).
Lúc khởi động bỏ qua các dòng cũ trong bộ đệm của gateway (không bắn lại tin đã đọc từ trước). Lỗi lặp thì nghỉ tăng dần tới
60 giây và chỉ ghi nhật ký MỘT lần cho mỗi kiểu lỗi (không ghi mỗi vòng)."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .api import TelegramCallAuthError, TelegramCallError, TelegramGatewayApi
from .bridge_core import message_event, redact, send_failed_event
from .const import EVENT_MESSAGE, EVENT_SEND_FAILED, SIGNAL_MESSAGE

_LOGGER = logging.getLogger(__name__)
LONG_POLL_S = 20
MAX_BACKOFF_S = 60
AUTH_RETRY_S = 300


class MessageFeed:
    def __init__(self, hass: HomeAssistant, api: TelegramGatewayApi, entry_id: str) -> None:
        self._hass, self._api, self._entry_id = hass, api, entry_id
        self.last: dict = {}                    # tin đến gần nhất (cho cảm biến)
        self.received = 0

    def handle(self, item: dict) -> None:
        ev = message_event(item)
        if ev is not None:
            self.last, self.received = ev, self.received + 1
            self._hass.bus.async_fire(EVENT_MESSAGE, ev)
            async_dispatcher_send(self._hass, SIGNAL_MESSAGE.format(self._entry_id))
            return
        fail = send_failed_event(item)
        if fail is not None:
            self._hass.bus.async_fire(EVENT_SEND_FAILED, fail)

    async def run(self) -> None:
        cursor: int | None = None
        backoff, last_err = 2, ""
        while True:
            try:
                data = await self._api.feed(cursor or 0, wait=0 if cursor is None else LONG_POLL_S)
                if cursor is None or data.get("reset"):
                    cursor = int(data.get("cursor") or 0)          # bỏ qua dòng cũ: chỉ báo tin MỚI từ lúc này
                    continue
                for item in data.get("items") or []:
                    self.handle(item)
                cursor = int(data.get("cursor") or cursor)
                backoff, last_err = 2, ""
            except asyncio.CancelledError:
                raise
            except TelegramCallAuthError:
                if last_err != "auth":
                    _LOGGER.warning("Gateway Telegram từ chối token: bộ nghe tin nhắn tạm dừng")
                    last_err = "auth"
                await asyncio.sleep(AUTH_RETRY_S)
            except (TelegramCallError, ValueError, TypeError) as err:
                text = redact(str(err))
                if text != last_err:
                    _LOGGER.warning("Bộ nghe tin nhắn Telegram lỗi: %s (sẽ thử lại)", text)
                    last_err = text
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF_S)
