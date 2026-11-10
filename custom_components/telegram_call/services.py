"""Dịch vụ của Telegram Call: gọi (make_call, answer_call, hangup_call) và nhắn tin (gửi chữ/ảnh/video/âm thanh/tin thoại/tệp,
sửa, xoá, chuyển tiếp, phản ứng, đánh dấu đã đọc, lịch sử, danh sách hội thoại, tìm, dịch, tải tệp về).

Người nhận: ``chat_id`` (số; âm = nhóm/kênh) HOẶC ``peer`` (@username, +số điện thoại, ID số người dùng — gateway tra ra chat_id)."""
from __future__ import annotations

import mimetypes
import os
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelegramCallError
from .bridge_core import redact
from .const import DOMAIN

MAX_FILE = 64 * 1024 * 1024                       # đúng trần của gateway
KINDS = ("photo", "video", "audio", "voice", "document")
FORMATS = ("plain", "markdown", "html")

_ENTRY = {vol.Optional("config_entry_id"): cv.string}
MAKE_CALL = vol.All(cv.has_at_least_one_key("user_id", "peer"), vol.Schema({
    **_ENTRY,
    vol.Optional("user_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("peer"): cv.string,        # @username, +số điện thoại hoặc ID số
    vol.Optional("video", default=False): cv.boolean,
    vol.Optional("mic"): cv.string, vol.Optional("mic_url"): cv.string, vol.Optional("camera"): cv.string,
    vol.Optional("speaker"): cv.string,
    vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
}))
CALL_ID = vol.Schema({**_ENTRY, vol.Optional("call_id"): cv.string})

_TARGET = {vol.Optional("chat_id"): vol.Coerce(int), vol.Optional("peer"): cv.string}
_FMT = {vol.Optional("format", default="plain"): vol.In(FORMATS)}
_REPLY = {vol.Optional("reply_to"): vol.All(vol.Coerce(int), vol.Range(min=1))}


def _target(schema: dict) -> vol.Schema:
    return vol.All(cv.has_at_least_one_key("chat_id", "peer"), vol.Schema({**_ENTRY, **_TARGET, **schema}))


def _ids(value: Any) -> list[int]:
    if isinstance(value, (list, tuple)):
        parts = value
    else:
        parts = str(value or "").replace(";", ",").split(",")
    out = [int(str(p).strip()) for p in parts if str(p).strip()]
    if not out or len(out) > 100:
        raise vol.Invalid("message_ids: cần 1 đến 100 số ID tin nhắn")
    return out


# Tương thích bản trước: dịch vụ send_message nhận ``text`` (nay là ``message``, vẫn nhận cả hai).
SEND = vol.All(cv.has_at_least_one_key("message", "text"), _target({
    vol.Optional("message"): cv.string, vol.Optional("text"): cv.string, **_REPLY, **_FMT}))
SEND_FILE = _target({vol.Required("file"): cv.string, vol.Optional("caption", default=""): cv.string, **_REPLY, **_FMT,
                     vol.Optional("kind", default="document"): vol.In(KINDS)})
SEND_VOICE = vol.All(cv.has_at_least_one_key("file", "message"), _target({
    vol.Optional("file"): cv.string, vol.Optional("message"): cv.string,         # file: tệp âm thanh; message: gateway đọc thành giọng (TTS)
    vol.Optional("voice", default=""): cv.string, vol.Optional("rate", default=""): cv.string, **_REPLY}))
EDIT = _target({vol.Required("message_id"): vol.Coerce(int), vol.Required("message"): cv.string, **_FMT})
DELETE = _target({vol.Required("message_ids"): _ids, vol.Optional("revoke", default=True): cv.boolean})
FORWARD = vol.All(cv.has_at_least_one_key("chat_id", "peer"), vol.Schema({
    **_ENTRY, **_TARGET, vol.Required("from_chat_id"): vol.Coerce(int), vol.Required("message_ids"): _ids}))
REACT = _target({vol.Required("message_id"): vol.Coerce(int), vol.Optional("emoji", default="👍"): cv.string,
                 vol.Optional("remove", default=False): cv.boolean})
READ = _target({vol.Optional("message_ids"): _ids})
HISTORY = _target({vol.Optional("limit", default=20): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
                   vol.Optional("before"): vol.All(vol.Coerce(int), vol.Range(min=1))})
CHATS = vol.Schema({**_ENTRY, vol.Optional("limit", default=30): vol.All(vol.Coerce(int), vol.Range(min=1, max=200))})
FIND = vol.Schema({**_ENTRY, vol.Required("query"): cv.string})
CONTACTS = vol.Schema({**_ENTRY, vol.Optional("query", default=""): cv.string})
TRANSLATE = vol.Schema({**_ENTRY, vol.Required("text"): cv.string, vol.Required("to_language"): cv.string})
DOWNLOAD = vol.Schema({**_ENTRY, vol.Required("file_id"): vol.Coerce(int), vol.Required("path"): cv.string})


def _entry(hass: HomeAssistant, call: ServiceCall) -> ConfigEntry:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    want = call.data.get("config_entry_id")
    if want:
        entries = [e for e in entries if e.entry_id == want]
    if len(entries) != 1:
        raise ServiceValidationError("Chưa có (hoặc có nhiều) tài khoản Telegram Call: chỉ rõ config_entry_id" if not want
                                     else f"Không có mục cấu hình {want}")
    return entries[0]


def _public(info: dict) -> dict:
    """Thông tin cuộc gọi trả cho người dùng/automation: không có nhật ký sự kiện (có thể kèm lỗi dài/URL)."""
    return {k: (redact(str(v)) if k == "error" else v) for k, v in info.items()
            if k in ("id", "user_id", "outgoing", "video", "state", "emojis", "error", "seconds")}


async def _read_source(hass: HomeAssistant, src: str) -> tuple[str, bytes]:
    """Đọc tệp từ đường dẫn HA (phải nằm trong thư mục được phép), URL http(s) hoặc media-source://. Trả (tên, nội dung)."""
    src = str(src).strip()
    if src.startswith("media-source://"):
        from homeassistant.components import media_source
        from homeassistant.components.media_player.browse_media import async_process_play_media_url
        try:
            item = await media_source.async_resolve_media(hass, src, None)
        except Exception as err:  # noqa: BLE001 - mọi lỗi của nguồn media đều thành lỗi nhập liệu rõ ràng
            raise ServiceValidationError(f"Không mở được {src}: {err}") from err
        src = async_process_play_media_url(hass, item.url)
    if src.startswith(("http://", "https://")):
        try:
            async with async_get_clientsession(hass).get(src) as r:
                if r.status != 200:
                    raise ServiceValidationError(f"Tải {redact(src)} lỗi HTTP {r.status}")
                data = await r.content.read(MAX_FILE + 1)
        except OSError as err:
            raise ServiceValidationError(f"Không tải được tệp: {redact(str(err))}") from err
        name = os.path.basename(src.split("?", 1)[0]) or "tep"
    else:
        if not hass.config.is_allowed_path(src):
            raise ServiceValidationError(f"Đường dẫn {src} không nằm trong thư mục được phép (allowlist_external_dirs)")

        def _read() -> bytes:
            with open(src, "rb") as fh:
                return fh.read(MAX_FILE + 1)
        try:
            data = await hass.async_add_executor_job(_read)
        except OSError as err:
            raise ServiceValidationError(f"Không đọc được {src}: {err}") from err
        name = os.path.basename(src)
    if len(data) > MAX_FILE:
        raise ServiceValidationError("Tệp lớn hơn 64 MB (trần của Telegram qua gateway)")
    if not data:
        raise ServiceValidationError("Tệp rỗng")
    return name, data


def _msg(m: dict) -> dict:
    """Tin nhắn trả cho người dùng/automation: không có đường dẫn cục bộ."""
    return {"message_id": m.get("id"), "chat_id": m.get("chat_id"), "kind": m.get("kind"), "text": m.get("text"),
            "date": m.get("date"), "outgoing": m.get("outgoing"), "sender": m.get("sender") or None}


def async_register_services(hass: HomeAssistant) -> None:
    async def run(call: ServiceCall, fn):
        try:
            return await fn(_entry(hass, call).runtime_data)
        except TelegramCallError as err:
            raise HomeAssistantError(redact(str(err))) from err

    async def make_call(call: ServiceCall) -> ServiceResponse:
        override = {k: call.data[k] for k in ("mic", "mic_url", "camera", "speaker", "volume") if k in call.data}

        async def go(rt):
            rt.bridge.remember(None, override)                     # loa/âm lượng riêng cuộc này, dùng lúc nghe máy
            fields = await rt.bridge.async_call_fields(override)
            return _public(await rt.api.live_call(call.data.get("user_id") or str(call.data["peer"]).strip(), call.data["video"], **{
                "mic": fields.get("mic"), "mic_format": fields.get("mic_format"), "camera": fields.get("camera"),
                "speaker": fields.get("speaker")}))
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def _pick(rt, call: ServiceCall, incoming: bool) -> str:
        """call_id đã cho, hoặc cuộc gọi DUY NHẤT phù hợp: (nghe) cuộc gọi đến đang đổ chuông, (cúp) cuộc gọi đang diễn ra."""
        if call.data.get("call_id"):
            return call.data["call_id"]
        live = rt.coordinator.live_calls()
        if incoming:
            live = [c for c in live if not c.get("outgoing") and c.get("state") == "ringing"]
        if len(live) != 1:
            raise ServiceValidationError("Cần call_id: " + ("không có đúng một cuộc gọi đến đang đổ chuông" if incoming
                                                            else "không có đúng một cuộc gọi đang diễn ra"))
        return str(live[0]["id"])

    async def answer_call(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return _public(await rt.api.accept(await _pick(rt, call, True)))
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def hangup_call(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return _public(await rt.api.hangup(await _pick(rt, call, False)))
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def chat_of(rt, d: dict) -> int:
        """chat_id đã cho, hoặc tra từ peer (@username, +số, ID số)."""
        if d.get("chat_id") is not None:
            return int(d["chat_id"])
        return await rt.api.resolve(d["peer"])

    async def send_message(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            text = call.data.get("message") or call.data.get("text")
            if not str(text).strip():
                raise ServiceValidationError("Nội dung tin nhắn rỗng")
            return _msg(await rt.api.send_text(await chat_of(rt, call.data), str(text), call.data.get("reply_to", 0), call.data["format"]))
        return await run(call, go)

    async def _send_media(call: ServiceCall, kind: str) -> ServiceResponse:
        async def go(rt):
            name, data = await _read_source(hass, call.data["file"])
            return _msg(await rt.api.send_file(await chat_of(rt, call.data), kind, name, data, call.data["caption"],
                                               call.data.get("reply_to", 0), call.data["format"]))
        return await run(call, go)

    async def send_file(call: ServiceCall) -> ServiceResponse:
        return await _send_media(call, call.data["kind"])

    def fixed_kind(kind: str):
        async def handler(call: ServiceCall) -> ServiceResponse:
            return await _send_media(call, kind)
        return handler

    async def send_voice(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            if call.data.get("file"):
                name, data = await _read_source(hass, call.data["file"])
                return _msg(await rt.api.send_file(await chat_of(rt, call.data), "voice", name, data, "", call.data.get("reply_to", 0)))
            chat_id = await chat_of(rt, call.data)
            if chat_id < 0:
                raise ServiceValidationError("Giọng đọc (message) hiện chỉ gửi được cho người dùng, chưa gửi cho nhóm/kênh; dùng file")
            res = await rt.api.send_voice_tts(str(chat_id), str(call.data["message"]), call.data["voice"], call.data["rate"])
            return {"message_id": res.get("tin_id"), "chat_id": chat_id, "kind": "voice"}
        return await run(call, go)

    async def edit_message(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return _msg(await rt.api.edit_message(await chat_of(rt, call.data), call.data["message_id"], call.data["message"], call.data["format"]))
        return await run(call, go)

    async def delete_message(call: ServiceCall) -> None:
        async def go(rt):
            await rt.api.delete_messages(await chat_of(rt, call.data), call.data["message_ids"], call.data["revoke"])
        await run(call, go)

    async def forward_message(call: ServiceCall) -> None:
        async def go(rt):
            await rt.api.forward_messages(await chat_of(rt, call.data), call.data["from_chat_id"], call.data["message_ids"])
        await run(call, go)

    async def add_reaction(call: ServiceCall) -> None:
        async def go(rt):
            await rt.api.react(await chat_of(rt, call.data), call.data["message_id"], call.data["emoji"], not call.data["remove"])
        await run(call, go)

    async def mark_read(call: ServiceCall) -> None:
        async def go(rt):
            await rt.api.mark_read(await chat_of(rt, call.data), call.data.get("message_ids") or [])
        await run(call, go)

    async def get_history(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            msgs = await rt.api.history(await chat_of(rt, call.data), call.data["limit"], call.data.get("before", 0))
            return {"messages": [{**_msg(m), "text": (m.get("text") or "")[:1000], "summary": m.get("summary")} for m in msgs]}
        return await run(call, go)

    async def get_chats(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return {"chats": [{k: c.get(k) for k in ("id", "title", "kind", "unread", "pinned")} | {"last": ((c.get("last") or {}).get("text"))}
                              for c in await rt.api.chats(call.data["limit"])]}
        return await run(call, go)

    async def find_chat(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return {"chats": [{k: c.get(k) for k in ("id", "title", "kind")} for c in await rt.api.search(call.data["query"])]}
        return await run(call, go)

    async def get_contacts(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            q = call.data["query"].strip().lower()
            rows = [c for c in await rt.api.contacts()
                    if not q or q in str(c.get("name", "")).lower() or q in str(c.get("username", "")).lower()]
            return {"contacts": [{"id": c.get("id"), "name": c.get("name"), "username": c.get("username")} for c in rows[:200]]}
        return await run(call, go)

    async def translate_text(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return {"text": await rt.api.translate(call.data["text"], call.data["to_language"])}
        return await run(call, go)

    async def download_file(call: ServiceCall) -> ServiceResponse:
        path = call.data["path"]
        if not hass.config.is_allowed_path(path):
            raise ServiceValidationError(f"Đường dẫn {path} không nằm trong thư mục được phép (allowlist_external_dirs)")

        async def go(rt):
            data = await rt.api.download_file(call.data["file_id"])

            def _write() -> None:
                with open(path, "wb") as fh:
                    fh.write(data)
            await hass.async_add_executor_job(_write)
            return {"path": path, "bytes": len(data)}
        return await run(call, go)

    R = SupportsResponse.OPTIONAL
    for name, fn, schema, resp in (("make_call", make_call, MAKE_CALL, R),
                                   ("answer_call", answer_call, CALL_ID, R),
                                   ("hangup_call", hangup_call, CALL_ID, R),
                                   ("send_message", send_message, SEND, R),
                                   ("send_file", send_file, SEND_FILE, R),
                                   ("send_photo", fixed_kind("photo"), SEND_FILE, R),
                                   ("send_video", fixed_kind("video"), SEND_FILE, R),
                                   ("send_audio", fixed_kind("audio"), SEND_FILE, R),
                                   ("send_voice", send_voice, SEND_VOICE, R),
                                   ("edit_message", edit_message, EDIT, R),
                                   ("delete_message", delete_message, DELETE, SupportsResponse.NONE),
                                   ("forward_message", forward_message, FORWARD, SupportsResponse.NONE),
                                   ("add_reaction", add_reaction, REACT, SupportsResponse.NONE),
                                   ("mark_read", mark_read, READ, SupportsResponse.NONE),
                                   ("get_history", get_history, HISTORY, SupportsResponse.ONLY),
                                   ("get_chats", get_chats, CHATS, SupportsResponse.ONLY),
                                   ("find_chat", find_chat, FIND, SupportsResponse.ONLY),
                                   ("get_contacts", get_contacts, CONTACTS, SupportsResponse.ONLY),
                                   ("translate_text", translate_text, TRANSLATE, SupportsResponse.ONLY),
                                   ("download_file", download_file, DOWNLOAD, R)):
        hass.services.async_register(DOMAIN, name, fn, schema=schema, supports_response=resp)


async def _refresh(hass: HomeAssistant, call: ServiceCall) -> None:
    try:
        await _entry(hass, call).runtime_data.coordinator.async_request_refresh()
    except ServiceValidationError:
        pass
