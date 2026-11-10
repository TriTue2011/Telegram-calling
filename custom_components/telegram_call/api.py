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
        extra = kw.pop("params", None) or {}
        params = {**({"account": self.account} if (account and self.account) else {}), **extra} or None
        kw.setdefault("timeout", TIMEOUT)
        try:
            async with self._s.request(method, self.url + path, headers=headers, params=params, **kw) as r:
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

    async def live_call(self, target: int | str, video: bool = False, mic: str | None = None,
                        mic_format: str | None = None, camera: str | None = None, speaker: str | None = None) -> dict:
        # target: ID số (user_id) hoặc chuỗi @username / +số điện thoại (gateway tra ra ID).
        body: dict[str, Any] = {"video": bool(video)}
        if isinstance(target, int) or str(target).strip().isdigit():
            body["user_id"] = int(target)
        else:
            body["peer"] = str(target).strip()
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

    # -- nhắn tin -----------------------------------------------------------------------------------------------
    async def resolve(self, peer: int | str) -> int:
        """@username, +số điện thoại, ID số (người dùng) hoặc số âm (nhóm/kênh) -> chat_id."""
        return int((await self._request("POST", "/api/resolve", json={"peer": str(peer).strip()}))["chat_id"])

    async def send_text(self, chat_id: int, text: str, reply_to: int = 0, parse_mode: str = "") -> dict:
        body: dict[str, Any] = {"text": text}
        if reply_to:
            body["reply_to"] = int(reply_to)
        if parse_mode and parse_mode != "plain":
            body["parse_mode"] = parse_mode
        return await self._request("POST", f"/api/chats/{int(chat_id)}/send", json=body)

    async def send_message(self, chat_id: int, text: str) -> dict:
        return await self.send_text(chat_id, text)

    async def send_file(self, chat_id: int, kind: str, name: str, data: bytes, caption: str = "", reply_to: int = 0,
                        parse_mode: str = "") -> dict:
        params: dict[str, Any] = {"kind": kind, "name": name}
        if caption:
            params["caption"] = caption
        if reply_to:
            params["reply_to"] = int(reply_to)
        if parse_mode and parse_mode != "plain":
            params["parse_mode"] = parse_mode
        return await self._request("POST", f"/api/chats/{int(chat_id)}/send_file", params=params, data=data,
                                   timeout=aiohttp.ClientTimeout(total=120))

    async def send_voice_tts(self, peer: str, text: str, voice: str = "", rate: str = "") -> dict:
        body: dict[str, Any] = {"peer": str(peer), "tts_text": text}
        if voice:
            body["tts_voice"] = voice
        if rate:
            body["tts_rate"] = rate
        return await self._request("POST", "/api/send_voice", json=body, timeout=aiohttp.ClientTimeout(total=90))

    async def edit_message(self, chat_id: int, message_id: int, text: str, parse_mode: str = "") -> dict:
        body: dict[str, Any] = {"chat_id": int(chat_id), "message_id": int(message_id), "text": text}
        if parse_mode and parse_mode != "plain":
            body["parse_mode"] = parse_mode
        return await self._request("POST", "/api/messages/edit", json=body)

    async def delete_messages(self, chat_id: int, message_ids: list[int], revoke: bool = True) -> None:
        await self._request("POST", "/api/messages/delete", json={"chat_id": int(chat_id), "message_ids": [int(i) for i in message_ids],
                                                                  "revoke": bool(revoke)})

    async def forward_messages(self, to_chat_id: int, from_chat_id: int, message_ids: list[int]) -> None:
        await self._request("POST", "/api/messages/forward", json={"to_chat_id": int(to_chat_id), "from_chat_id": int(from_chat_id),
                                                                   "message_ids": [int(i) for i in message_ids]})

    async def react(self, chat_id: int, message_id: int, emoji: str, on: bool = True) -> None:
        await self._request("POST", "/api/messages/react", json={"chat_id": int(chat_id), "message_id": int(message_id),
                                                                 "emoji": emoji, "on": bool(on)})

    async def mark_read(self, chat_id: int, message_ids: list[int]) -> None:
        await self._request("POST", f"/api/chats/{int(chat_id)}/read", json={"message_ids": [int(i) for i in message_ids]})

    async def history(self, chat_id: int, limit: int = 20, before: int = 0) -> list[dict]:
        params = {"limit": int(limit)}
        if before:
            params["before"] = int(before)
        return (await self._request("GET", f"/api/chats/{int(chat_id)}/messages", params=params)).get("messages", [])

    async def chats(self, limit: int = 30) -> list[dict]:
        return (await self._request("GET", "/api/chats", params={"limit": int(limit)})).get("chats", [])

    async def contacts(self) -> list[dict]:
        return (await self._request("GET", "/api/contacts")).get("contacts", [])

    async def search(self, query: str) -> list[dict]:
        return (await self._request("GET", "/api/search", params={"q": query})).get("chats", [])

    async def translate(self, text: str, to_language_code: str) -> str:
        return str((await self._request("POST", "/api/translate", json={"text": text, "to_language_code": to_language_code})).get("text", ""))

    async def download_file(self, file_id: int) -> bytes:
        try:
            async with self._s.get(self.url + f"/api/file/{int(file_id)}", headers={"Authorization": f"Bearer {self._token}"},
                                   params={"account": self.account} if self.account else None,
                                   timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status == 401:
                    raise TelegramCallAuthError("Gateway từ chối token", 401)
                if r.status >= 400:
                    raise TelegramCallError(f"HTTP {r.status}", r.status)
                return await r.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise TelegramCallError(f"Không gọi được tới gateway: {redact(str(err))}") from err

    async def feed(self, since: int, wait: int = 20) -> dict:
        return await self._request("GET", "/api/feed", params={"since": int(since), "wait": int(wait)},
                                   timeout=aiohttp.ClientTimeout(total=wait + 15))
