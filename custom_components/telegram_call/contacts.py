"""Danh bạ Telegram (lấy từ gateway, nhớ vài phút) để cuộc gọi đến biết người gọi là «quen» (có trong danh bạ) hay «lạ», và tên đã lưu — cho quy tắc theo phạm vi
``contacts`` / ``strangers`` và để đọc tên người gọi ra loa. Chưa tải được danh bạ thì trạng thái là ``unknown`` (không đoán)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .api import TelegramCallError

_LOGGER = logging.getLogger(__name__)
TTL_S = 300.0
FETCH_TIMEOUT_S = 2.5


class ContactBook:
    def __init__(self, api: Any) -> None:
        self._api = api
        self._rows: dict[int, dict] | None = None
        self._at = 0.0

    async def _refresh(self) -> None:
        try:
            rows = await asyncio.wait_for(self._api.contacts(), FETCH_TIMEOUT_S)
        except (TelegramCallError, asyncio.TimeoutError):
            _LOGGER.debug("Chưa tải được danh bạ Telegram, dùng bản cũ (nếu có)")
            self._at = time.monotonic() - TTL_S + 30       # lỗi thì thử lại sau ~30 giây, không dồn mỗi lần hỏi
            return
        self._rows = {int(r["id"]): r for r in rows if str(r.get("id", "")).lstrip("-").isdigit()}
        self._at = time.monotonic()

    async def async_lookup(self, user_id: Any) -> tuple[str, str]:
        """(trạng thái, tên): friend / stranger / unknown; tên đã lưu hoặc rỗng."""
        if self._rows is None or time.monotonic() - self._at > TTL_S:
            await self._refresh()
        if self._rows is None:
            return "unknown", ""
        try:
            row = self._rows.get(int(user_id))
        except (TypeError, ValueError):
            return "unknown", ""
        return ("friend", str(row.get("name") or "")) if row else ("stranger", "")

    def names(self) -> dict[int, str]:
        return {k: str(v.get("name") or "") for k, v in (self._rows or {}).items()}
