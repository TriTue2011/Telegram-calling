"""Máy khách HTTP của telegram-gateway (cuộc gọi thật đi qua ``/api/live/*``).

Chỉ dùng các route THẬT (TDLib + NTgCalls). Các route ``/api/call`` / ``/api/answer_call`` / ``/api/hangup_call`` /
``/api/audio/*`` của gateway là engine giả lập cũ — tích hợp này không bao giờ gọi chúng."""
from __future__ import annotations

import asyncio
from typing import Any

import aiohttp

from .bridge_core import redact

TIMEOUT = aiohttp.ClientTimeout(total=30)


class TelegramCallError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class TelegramCallAuthError(TelegramCallError):
    """Gateway từ chối token (401)."""


class TelegramGatewayApi:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str, account: str = "") -> None:
        self._s = session
        self.url = url.rstrip("/")
        self._token = token
        self.account = account

    async def _request(self, method: str, path: str, *, auth: bool = True, account: bool = True, **kw: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token}"} if auth else {}
        params = {"account": self.account} if (account and self.account) else None
        try:
            async with self._s.request(method, self.url + path, headers=headers, params=params, timeout=TIMEOUT, **kw) as r:
                try:
                    data = await r.json(content_type=None)
                except (aiohttp.ContentTypeError, ValueError):
                    data = {}
                if r.status == 401:
                    raise TelegramCallAuthError("Gateway từ chối token", 401)
                if r.status >= 400:
                    msg = ""
                    if isinstance(data, dict):
                        err = data.get("error")
                        msg = (err.get("message") if isinstance(err, dict) else err) or data.get("detail") or ""
                    raise TelegramCallError(redact(str(msg)) or f"HTTP {r.status}", r.status)
                return data
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise TelegramCallError(f"Không gọi được tới gateway: {redact(str(err))}") from err

    async def health(self) -> dict:
        return await self._request("GET", "/health", auth=False, account=False)

    async def accounts(self) -> list[dict]:
        return (await self._request("GET", "/api/accounts", account=False)).get("tai_khoan", [])

    async def live_calls(self) -> list[dict]:
        return (await self._request("GET", "/api/live/calls")).get("calls", [])

    async def live_call(self, user_id: int, video: bool = False, mic: str | None = None,
                        mic_format: str | None = None, camera: str | None = None, speaker: str | None = None) -> dict:
        body: dict[str, Any] = {"user_id": int(user_id), "video": bool(video)}
        for k, v in (("mic", mic), ("mic_format", mic_format), ("camera", camera), ("speaker", speaker)):
            if v:
                body[k] = v
        return await self._request("POST", "/api/live/call", json=body)

    async def accept(self, call_id: str) -> dict:
        return await self._request("POST", "/api/live/accept", json={"id": call_id})

    async def hangup(self, call_id: str) -> dict:
        return await self._request("POST", "/api/live/hangup", json={"id": call_id})

    async def set_live_settings(self, patch: dict[str, Any]) -> None:
        """Đặt thiết bị MẶC ĐỊNH của gateway (``mic``, ``mic_format``, ``camera``, ``speaker``). Trường vắng = gateway giữ giá trị cũ."""
        await self._request("POST", "/api/live/settings", json=patch)

    async def send_message(self, chat_id: int, text: str) -> dict:
        return await self._request("POST", f"/api/chats/{int(chat_id)}/send", json={"text": text})
