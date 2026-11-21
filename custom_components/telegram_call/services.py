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

from . import call_rules, catalog
from .api import TelegramCallError
from .accounts import select_entry
from .rules import (CALL_TYPES, CONNECT, MAX_ANNOUNCE_REPEAT, MAX_NOTE_LEN, RULES_KEY, SCOPES, VIDEO_REQUEST, RuleError, get_rules, normalize as normalize_rule,
                    parse_tags, validate_scope)
from .bridge_core import redact
from .const import DOMAIN

MAX_FILE = 64 * 1024 * 1024                       # đúng trần của gateway
KINDS = ("photo", "video", "audio", "voice", "document")
FORMATS = ("plain", "markdown", "html")

_ENTRY = {vol.Optional("config_entry_id"): cv.string, vol.Optional("account"): cv.string}
_PARTS = vol.All(cv.ensure_list, [vol.In(["mic", "speaker", "camera"])])       # ô tích: mic / loa / camera (như connect_now, connect của quy tắc)
MAKE_CALL = vol.All(cv.has_at_least_one_key("user_id", "peer"), vol.Schema({
    **_ENTRY,
    vol.Optional("user_id"): vol.All(vol.Coerce(int), vol.Range(min=1)),
    vol.Optional("peer"): cv.string,        # @username, +số điện thoại hoặc ID số
    vol.Optional("video", default=False): cv.boolean,
    vol.Optional("mic"): cv.string, vol.Optional("mic_url"): cv.string, vol.Optional("camera"): cv.string,
    vol.Optional("speaker"): cv.string,
    vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    vol.Optional("connect_now"): _PARTS, vol.Optional("connect"): _PARTS,
    vol.Optional("message"): cv.string, vol.Optional("hangup_after", default=False): cv.boolean,       # lời nhắn đọc khi nghe máy (như make_call của zalo_call)
    vol.Optional("pipeline"): cv.string, vol.Optional("tts_engine"): cv.entity_id, vol.Optional("language"): cv.string,
    vol.Optional("repeat", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
}))
CALL_ID = vol.Schema({**_ENTRY, vol.Optional("call_id"): cv.string})
ANSWER_CALL = vol.Schema({**_ENTRY, vol.Optional("call_id"): cv.string,                  # thiết bị riêng cho cuộc gọi này (như make_call)
                          vol.Optional("mic"): cv.string, vol.Optional("mic_url"): cv.string, vol.Optional("camera"): cv.string,
                          vol.Optional("speaker"): cv.string,
                          vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
                          vol.Optional("connect_now"): _PARTS, vol.Optional("connect"): _PARTS})

_RULE_FIELDS = ("name", "answer", "answer_delay", "call_types", "pipeline", "greeting", "hangup_after", "announce", "announce_to", "announce_text", "announce_repeat",
                "announce_volume", "connect_now", "connect", "video_request", "speaker", "mic", "mic_url", "camera", "volume")
_RULE = {vol.Optional("name"): cv.string, vol.Optional("answer"): cv.boolean,
         vol.Optional("answer_delay"): vol.All(vol.Coerce(int), vol.Range(min=0, max=120)),
         vol.Optional("call_types"): vol.All(cv.ensure_list, [vol.In(list(CALL_TYPES))]),
         vol.Optional("pipeline"): cv.string, vol.Optional("greeting"): cv.string, vol.Optional("hangup_after"): cv.boolean,
         vol.Optional("announce"): cv.boolean, vol.Optional("announce_to"): cv.string, vol.Optional("announce_text"): cv.string,
         vol.Optional("announce_repeat"): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_ANNOUNCE_REPEAT)),
         vol.Optional("announce_volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
         vol.Optional("connect_now"): vol.All(cv.ensure_list, [vol.In(list(CONNECT))]),
         vol.Optional("connect"): vol.All(cv.ensure_list, [vol.In(list(CONNECT))]),
         vol.Optional("video_request"): vol.In(list(VIDEO_REQUEST)),
         vol.Optional("speaker"): cv.string, vol.Optional("mic"): cv.string, vol.Optional("mic_url"): cv.string, vol.Optional("camera"): cv.string,
         vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100))}
_SCOPE_KEY = {vol.Optional("thread_id"): vol.Any(int, cv.string), vol.Optional("tag"): cv.string}
SET_RULE = vol.Schema({**_ENTRY, vol.Required("scope"): vol.In(list(SCOPES)), **_SCOPE_KEY, **_RULE})
APPLY_RULE = vol.Schema({**_ENTRY, vol.Required("call_id"): vol.Any(int, cv.string), vol.Optional("scope", default="thread"): vol.In(list(SCOPES)),
                         vol.Optional("source"): cv.string, **_RULE})
REMOVE_RULE = vol.Schema({**_ENTRY, vol.Required("scope"): vol.In(list(SCOPES)), **_SCOPE_KEY})
CONTACT_TAGS = vol.Schema({**_ENTRY, vol.Required("thread_id"): vol.Any(int, cv.string), vol.Optional("tags"): vol.Any(cv.string, [cv.string]),
                           vol.Optional("note"): cv.string})
CALL_ONLY = vol.Schema({**_ENTRY, vol.Optional("call_id"): cv.string})
ACCEPT_VIDEO = vol.Schema({**_ENTRY, vol.Optional("call_id"): cv.string, vol.Optional("camera"): cv.string})
SAY_IN_CALL = vol.Schema({**_ENTRY, vol.Optional("call_id"): cv.string, vol.Required("message"): cv.string, vol.Optional("tts_engine"): cv.entity_id,
                          vol.Optional("language"): cv.string, vol.Optional("pipeline"): cv.string, vol.Optional("wait", default=True): cv.boolean})
ALERT_CALL = vol.All(cv.has_at_least_one_key("user_id", "peer"), vol.Schema({
    **_ENTRY, vol.Optional("user_id"): vol.All(vol.Coerce(int), vol.Range(min=1)), vol.Optional("peer"): cv.string,
    vol.Required("message"): cv.string, vol.Optional("video", default=False): cv.boolean,
    vol.Optional("after"): vol.In(["hangup", "connect", "wait"]), vol.Optional("pipeline"): cv.string, vol.Optional("tts_engine"): cv.entity_id,
    vol.Optional("language"): cv.string, vol.Optional("repeat", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
    vol.Optional("mic"): cv.string, vol.Optional("mic_url"): cv.string, vol.Optional("camera"): cv.string, vol.Optional("speaker"): cv.string,
    vol.Optional("volume"): vol.All(vol.Coerce(int), vol.Range(min=0, max=100))}))
NO_ARGS = vol.Schema({**_ENTRY})


def _int_list(value: Any) -> list[int]:
    """Danh sách số: «1, 2, 3», danh sách hoặc một số; rỗng là hợp lệ (vd rút phiếu bình chọn)."""
    parts = value if isinstance(value, (list, tuple)) else str(value if value is not None else "").replace(";", ",").split(",")
    try:
        return [int(str(p).strip()) for p in parts if str(p).strip()]
    except ValueError:
        raise vol.Invalid("cần danh sách số nguyên, cách nhau bằng dấu phẩy")


def _lines(value: Any) -> list[str]:
    """Mỗi dòng một mục (ô văn bản nhiều dòng) hoặc danh sách chuỗi."""
    items = value if isinstance(value, (list, tuple)) else [value]
    return [ln.strip() for it in items for ln in str(it).splitlines() if ln.strip()]


def _field_validator(f: catalog.F) -> Any:
    if f.kind == "int":
        return vol.All(vol.Coerce(int), vol.Range(min=f.lo, max=f.hi))
    if f.kind == "bool":
        return cv.boolean
    if f.kind == "list_int":
        return _int_list
    if f.kind == "list_text":
        return _lines
    if f.kind == "select":
        return vol.In(f.options)
    return cv.string


def _catalog_schema(spec: catalog.S) -> Any:
    """Schema của một dịch vụ trong ``catalog.SERVICES``: ô riêng + ô nhắm (cuộc trò chuyện hoặc người) như các dịch vụ khác."""
    fields: dict = {}
    for f in spec.fields:
        key = vol.Required(f.key) if f.required else (vol.Optional(f.key, default=f.default) if f.default is not None else vol.Optional(f.key))
        fields[key] = _field_validator(f)
    if spec.target == "chat":
        return _target(fields)
    if spec.target == "user":
        user_keys = {} if any(f.key == "user_id" for f in spec.fields) else {vol.Optional("user_id"): vol.All(vol.Coerce(int), vol.Range(min=1))}
        return vol.All(cv.has_at_least_one_key("user_id", "peer"), vol.Schema({**_ENTRY, **user_keys, vol.Optional("peer"): cv.string, **fields}))
    return vol.Schema({**_ENTRY, **fields})

# Tương thích với dịch vụ gửi tin của ``telegram_bot`` của HA: cùng một mẫu dữ liệu dùng được cho cả hai (blueprint gửi qua bot và qua Telegram Call).
# ``message_thread_id`` (chủ đề trong nhóm) chưa hỗ trợ nên bỏ qua; ``parse_mode`` đổi thành ``format``.
_TARGET = {vol.Optional("chat_id"): vol.Coerce(int), vol.Optional("peer"): cv.string,
           vol.Optional("message_thread_id"): vol.Any(None, "", vol.Coerce(int)), vol.Optional("parse_mode"): cv.string,
           vol.Optional("target"): vol.Any(vol.Coerce(int), [vol.Coerce(int)])}       # ``telegram_bot.send_message`` dùng ``target`` (số hoặc danh sách) thay cho chat_id
_ALBUM_GROUP = {"photo": "media", "video": "media", "document": "document", "audio": "audio"}       # Telegram không cho trộn các nhóm trong một album
_PARSE_MODES = {"plain_text": "plain", "plain": "plain", "markdown": "markdown", "markdownv2": "markdown", "html": "html"}


def _compat(data: dict) -> dict:
    """Mẫu dữ liệu kiểu ``telegram_bot`` → của tích hợp: ``parse_mode`` → ``format`` (chuỗi lạ thì ``plain``); ``target`` (số hoặc danh sách; chỉ lấy
    người/nhóm ĐẦU TIÊN) → ``chat_id`` khi chưa có chat_id/peer. Chạy TRƯỚC khi kiểm lược đồ."""
    data = dict(data)
    if data.get("parse_mode"):
        data["format"] = _PARSE_MODES.get(str(data["parse_mode"]).lower(), "plain")
    tgt = data.pop("target", None)
    if tgt not in (None, "", []) and "chat_id" not in data and "peer" not in data:
        data["chat_id"] = tgt[0] if isinstance(tgt, (list, tuple)) else tgt
    return data
_FMT = {vol.Optional("format", default="plain"): vol.In(FORMATS)}
_REPLY = {vol.Optional("reply_to"): vol.All(vol.Coerce(int), vol.Range(min=1))}


def _target(schema: dict) -> vol.Schema:
    return vol.All(_compat, cv.has_at_least_one_key("chat_id", "peer"), vol.Schema({**_ENTRY, **_TARGET, **schema}))


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
MEDIA_GROUP = _target({vol.Required("media"): vol.All(cv.ensure_list, [vol.Schema({
    vol.Optional("media_type", default="photo"): vol.In(KINDS), vol.Required("file"): cv.string, vol.Optional("caption", default=""): cv.string,
    vol.Optional("parse_mode"): cv.string}, extra=vol.ALLOW_EXTRA)]), **_REPLY, **_FMT})
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
    try:
        return select_entry(entries, call.data.get("config_entry_id", ""), call.data.get("account", ""))
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err


def _schema_keys(schema: Any) -> set[str]:
    """Tên các khoá khai trong một schema voluptuous (đi vào cả vol.All / vol.Schema lồng)."""
    out: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, vol.Schema):
            walk(x.schema)
        elif isinstance(x, dict):
            out.update(str(k) for k in x)
        elif isinstance(x, vol.All):
            for v in x.validators:
                walk(v)
    walk(schema)
    return out


_RECIPIENT_KEYS = ("user_id", "chat_id", "thread_id")      # ô «ID» mà một người nhận chọn từ danh bạ sẽ điền thay (chỉ ô mà dịch vụ có khai)


def _with_recipient(hass: HomeAssistant, schema: Any, fill: tuple[str, ...] | None = None) -> Any:
    """Ô ``recipient``: chọn NGƯỜI từ danh bạ (thực thể notify của Telegram Call) thay cho việc dán ID; người ngoài danh bạ vẫn dùng ``peer`` (@username, +số điện thoại) hoặc ID số.
    Giải trước khi kiểm schema nên các ràng buộc «ít nhất một trong chat_id / peer» vẫn đúng; ô khai tường minh luôn thắng ô điền từ người nhận."""
    targets = [k for k in (_RECIPIENT_KEYS if fill is None else fill) if k in _schema_keys(schema)]
    if not targets:
        return schema

    def resolve(data: dict) -> dict:
        rid = data.get("recipient")
        if rid in (None, ""):
            return {k: v for k, v in data.items() if k != "recipient"}
        out = {k: v for k, v in data.items() if k != "recipient"}
        st = hass.states.get(str(rid))
        uid = (st.attributes.get("user_id") if st is not None else None) or ""
        if not str(uid).strip().lstrip("-").isdigit():
            raise vol.Invalid("Không tìm thấy người nhận «%s»: chọn một thực thể notify của Telegram Call (danh bạ) hoặc nhập peer / ID." % rid)
        for k in targets:
            out.setdefault(k, str(uid) if k == "thread_id" else int(uid))
        return out
    return vol.All(resolve, schema)


_PART_LABEL = {"mic": "Mic", "speaker": "Loa", "camera": "Camera"}


def _require_devices(fields: dict[str, str], now: list[str] | None, video: bool) -> None:
    """Gọi đi KHÔNG khai thiết bị mà cũng chưa đặt mặc định thì báo lỗi, không gọi một cuộc im lặng / hình đen mà người dùng không để ý. Thứ tự nguồn thiết bị: ô khai trong lần gọi dịch vụ →
    quy tắc automation (cuộc gọi ĐẾN) → thiết bị mặc định trên thiết bị «Telegram Call». ``connect_now`` = [] (chủ động trống) là cách xin gọi im lặng.
    Phần đã tích mà không có thiết bị → lỗi nêu đúng phần còn thiếu."""
    have = {"mic": bool(fields.get("mic")), "speaker": bool(fields.get("speaker")), "camera": bool(fields.get("camera"))}
    if now is not None:
        wanted = list(now) + (["camera"] if video and "camera" not in now else [])
        missing = [p for p in wanted if not have[p]]
    else:                                                          # không nêu connect_now: cần ít nhất một đường nói chuyện (mic hoặc loa), và camera nếu gọi video
        missing = (["camera"] if video and not have["camera"] else [])
        if not (have["mic"] or have["speaker"]):
            missing.append("mic hoặc loa")
    if missing:
        names = ", ".join(_PART_LABEL.get(p, p) for p in missing)
        raise ServiceValidationError(
            "Chưa có thiết bị để nối: %s. Điền ô tương ứng trong lời gọi, hoặc chọn thiết bị mặc định trên thiết bị «Telegram Call» (Mic / Loa / Camera). "
            "Muốn gọi im lặng thì để ô «Nối ngay khi nghe máy» trống (connect_now: [])." % names)


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

    def _own_rule(call: ServiceCall) -> dict:
        """Quy tắc của RIÊNG cuộc gọi này từ các ô của dịch vụ (thiết bị + «nối ngay» + «khi nói trực tiếp»): cho take_over_call đọc lại giữa cuộc gọi."""
        rule = {k: call.data[k] for k in ("mic", "mic_url", "camera", "speaker", "volume") if k in call.data}
        rule["connect_now"], rule["connect"] = list(call.data.get("connect_now") or []), list(call.data.get("connect") or [])
        return rule

    async def make_call(call: ServiceCall) -> ServiceResponse:
        message = str(call.data.get("message") or "").strip()
        if message:                                                # có lời nhắn: đọc khi nghe máy rồi cúp (hangup_after) hoặc nối thiết bị nhà
            return await _alert(call, message, "hangup" if call.data["hangup_after"] else "connect")
        override = {k: call.data[k] for k in ("mic", "mic_url", "camera", "speaker", "volume") if k in call.data}
        now = call.data.get("connect_now")                                 # có ô này: CHỈ nối thứ đã tích lúc nghe máy; không có: nối mọi thiết bị đã chọn/mặc định như trước
        if now is not None:
            override = call_rules.rule_override(override, list(now))
        video = bool(call.data["video"]) or "camera" in (now or [])         # tích camera = cuộc gọi VIDEO (như zalo_call)

        async def go(rt):
            rt.bridge.remember(None, override)                     # loa/âm lượng riêng cuộc này, dùng lúc nghe máy
            fields = await rt.bridge.async_call_fields(override)
            _require_devices(fields, now, video)
            if now is not None:                                    # thứ không tích: mic im lặng, hình đen, không loa (không rơi về thiết bị mặc định của gateway)
                fields.setdefault("mic", "silence")
                fields.setdefault("camera", "black")
                fields.setdefault("speaker", "")
            res = _public(await rt.api.live_call(call.data.get("user_id") or str(call.data["peer"]).strip(), video, **{
                "mic": fields.get("mic"), "mic_format": fields.get("mic_format"), "camera": fields.get("camera"),
                "speaker": fields.get("speaker")}))
            if now is not None or call.data.get("connect"):        # «khi nói trực tiếp» (take_over_call) dùng đúng thiết bị này của cuộc gọi
                rt.voice.rule_of[str(res["id"])] = _own_rule(call)
            return res
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
        override = {k: call.data[k] for k in ("mic", "mic_url", "camera", "speaker", "volume") if k in call.data}
        now = call.data.get("connect_now")
        if now is not None:                                       # chỉ nối thứ đã tích lúc nghe máy; còn lại im lặng / hình đen
            override = call_rules.rule_override(override, list(now))

        async def go(rt):
            cid = await _pick(rt, call, True)
            if override:
                await rt.bridge.async_apply_override(cid, override, held=now is not None)      # loa/mic/âm lượng riêng cuộc này, đặt TRƯỚC khi nghe máy
            if now is not None or call.data.get("connect"):
                rt.voice.rule_of[cid] = _own_rule(call)
            return _public(await rt.api.accept(cid))
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def hangup_call(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return _public(await rt.api.hangup(await _pick(rt, call, False)))
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def end_or_reject(call: ServiceCall, incoming: bool) -> ServiceResponse:
        async def go(rt):
            cid = await _pick(rt, call, incoming)
            return _public(await rt.api.hangup(cid))              # cúp một cuộc gọi đến đang đổ chuông = từ chối
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def reject_call(call: ServiceCall) -> ServiceResponse:
        return await end_or_reject(call, True)

    async def take_over_call(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return await call_rules.async_take_over(hass, rt, call.data.get("call_id"))
        return await run(call, go)

    def _mute(target: str):
        async def handler(call: ServiceCall) -> ServiceResponse:
            async def go(rt):
                return await call_rules.async_mute(rt, call.data.get("call_id"), target)
            return await run(call, go)
        return handler

    def _start(target: str):
        async def handler(call: ServiceCall) -> ServiceResponse:
            async def go(rt):
                cid = await call_rules._active_call_id(rt, call.data.get("call_id"))
                res = await rt.bridge.async_resolve(rt.bridge.devices_for())
                if target == "mic":
                    await rt.api.live_media(cid, mic=res["mic_url"] or "silence", mic_format=res["mic_format"])
                else:                                                         # bật video (cả khi cuộc đang là thoại): hình camera mặc định, không có thì hình đen
                    await rt.api.live_media(cid, video=True, camera=res["camera_url"] or "black", camera_off=False)
                return {"call_id": cid, "started": target}
            return await run(call, go)
        return handler

    def _tts_of(rt_call: ServiceCall) -> tuple:
        engine = rt_call.data.get("tts_engine")
        language, options = rt_call.data.get("language"), None
        if rt_call.data.get("pipeline"):
            p_engine, p_lang, options = call_rules.pipeline_tts(hass, rt_call.data["pipeline"])
            engine, language = engine or p_engine, language or p_lang
        return engine, language, options

    async def accept_video(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            return await call_rules.async_accept_video(hass, rt, call.data.get("call_id"), call.data.get("camera") or "")
        return await run(call, go)

    async def say_in_call(call: ServiceCall) -> ServiceResponse:
        async def go(rt):
            cid = await call_rules._active_call_id(rt, call.data.get("call_id"))
            engine, language, options = _tts_of(call)
            seconds = await call_rules.async_say(hass, rt, cid, call.data["message"], engine, language, options, wait=call.data["wait"])
            return {"call_id": cid, "seconds": seconds}
        return await run(call, go)

    async def _alert(call: ServiceCall, message: str, after: str) -> ServiceResponse:
        """Cuộc gọi có LỜI NHẮN: chờ nghe máy, đọc `message`, rồi làm theo `after` (cúp / nối thiết bị đã chọn / giữ máy). Dùng chung cho alert_call và make_call có `message`."""
        override = {k: call.data[k] for k in ("mic", "mic_url", "camera", "speaker", "volume") if k in call.data}

        async def go(rt):
            held = call_rules.rule_override({}, [])                     # lúc nghe máy mọi thiết bị nhà TẮT; chỉ nối sau khi đọc cảnh báo
            rt.bridge.remember(None, held)
            fields = await rt.bridge.async_call_fields(held)
            fields = {**fields, "mic": fields.get("mic") or "silence"}
            if call.data["video"]:
                fields["camera"] = fields.get("camera") or "black"
            info = await rt.api.live_call(call.data.get("user_id") or str(call.data["peer"]).strip(), call.data["video"], **{
                "mic": fields.get("mic"), "mic_format": fields.get("mic_format"), "camera": fields.get("camera"), "speaker": ""})
            engine, language, options = _tts_of(call)
            cid = str(info.get("id"))
            rt.voice.tasks[cid] = hass.async_create_task(call_rules.async_run_alert(
                hass, rt, cid, info.get("user_id"), message, call.data["repeat"], after, engine, language, options, override or None))
            return _public(info)
        res = await run(call, go)
        await _refresh(hass, call)
        return res

    async def alert_call(call: ServiceCall) -> ServiceResponse:
        message = str(call.data["message"]).strip()
        if not message:
            raise ServiceValidationError("message (nội dung cảnh báo) không được trống")
        return await _alert(call, message, call.data.get("after") or "")

    # -- quy tắc (cùng mô hình với zalo_call) ---------------------------------------------------------------
    def _rules_copy(rt) -> dict:
        r = get_rules(rt.defaults)
        return {"threads": {k: dict(v) for k, v in r["threads"].items()}, "contacts": dict(r["contacts"]), "strangers": dict(r["strangers"]),
                "tags": {k: dict(v) for k, v in r["tags"].items()}, "contact_meta": {k: dict(v) for k, v in r["contact_meta"].items()}}

    def _rule_of(call: ServiceCall) -> dict:
        data = {k: call.data[k] for k in _RULE_FIELDS if k in call.data}
        try:
            return normalize_rule(data, None)
        except RuleError as err:
            raise ServiceValidationError(str(err)) from err

    def _key(call: ServiceCall) -> str:
        try:
            return validate_scope(call.data["scope"], call.data.get("thread_id"), call.data.get("tag"))
        except RuleError as err:
            raise ServiceValidationError(str(err)) from err

    async def set_rule(call: ServiceCall) -> ServiceResponse:
        """Quy tắc LƯU SẴN (bản dự phòng khi không automation nào áp quy tắc). Trường không nêu thì giữ như cũ; quy tắc mới: mặc định TẮT hết."""
        rt = _entry(hass, call).runtime_data
        scope, key = call.data["scope"], _key(call)
        rules = _rules_copy(rt)
        old = rules["threads"].get(key) if scope == "thread" else rules["tags"].get(key) if scope == "tag" else (rules[scope] or None)
        try:
            rule = normalize_rule({k: call.data[k] for k in _RULE_FIELDS if k in call.data}, old)
        except RuleError as err:
            raise ServiceValidationError(str(err)) from err
        if scope == "thread":
            rules["threads"][key] = rule
        elif scope == "tag":
            rules["tags"][key] = rule
        else:
            rules[scope] = rule
        await rt.defaults.async_set(**{RULES_KEY: rules})
        return {"scope": scope, "thread_id": key if scope == "thread" else "", "tag": key if scope == "tag" else "", "rule": rule}

    async def apply_call_rule(call: ServiceCall) -> ServiceResponse:
        """Một automation áp quy tắc của nó cho MỘT cuộc gọi đang diễn ra (gọi từ automation bắt sự kiện telegram_call_call_state). Không lưu gì: quy tắc chỉ có hiệu lực cho
        cuộc gọi này, nên tắt/xoá automation là hết quy tắc. Nhiều automation cùng khớp: quy tắc cụ thể hơn (scope) thay quy tắc kém hơn; cùng bậc thì mỗi quy tắc báo ra
        loa của riêng nó, còn nghe máy/thiết bị/lời nhắn theo quy tắc đầu tiên bật `answer`."""
        rt = _entry(hass, call).runtime_data
        scope = call.data["scope"]
        status = await call_rules.async_apply_claim(hass, rt, call.data["call_id"], call_rules.Claim(
            _rule_of(call), SCOPES.index(scope), str(call.data.get("source") or "")))
        return {"call_id": str(call.data["call_id"]), "scope": scope, "status": status}

    async def remove_rule(call: ServiceCall) -> ServiceResponse:
        rt = _entry(hass, call).runtime_data
        scope, key = call.data["scope"], _key(call)
        rules = _rules_copy(rt)
        if scope == "thread":
            removed = rules["threads"].pop(key, None) is not None
        elif scope == "tag":
            removed = rules["tags"].pop(key, None) is not None
        else:
            removed = bool(rules[scope])
            rules[scope] = {}
        await rt.defaults.async_set(**{RULES_KEY: rules})
        return {"scope": scope, "thread_id": key if scope == "thread" else "", "tag": key if scope == "tag" else "", "removed": removed}

    async def list_rules(call: ServiceCall) -> ServiceResponse:
        r = get_rules(_entry(hass, call).runtime_data.defaults)
        return {"threads": r["threads"], "contacts": r["contacts"] or None, "strangers": r["strangers"] or None, "tags": r["tags"], "contact_meta": r["contact_meta"]}

    async def set_contact_tags(call: ServiceCall) -> ServiceResponse:
        """Gắn thẻ và/hoặc ghi chú cho một người. Thẻ có quy tắc riêng (set_rule scope = tag) thì mọi người mang thẻ đó dùng quy tắc ấy."""
        rt = _entry(hass, call).runtime_data
        try:
            uid = validate_scope("thread", call.data["thread_id"])
        except RuleError as err:
            raise ServiceValidationError(str(err)) from err
        rules = _rules_copy(rt)
        meta = dict(rules["contact_meta"].get(uid) or {})
        if "tags" in call.data:
            meta["tags"] = parse_tags(call.data["tags"])
        if "note" in call.data:
            meta["note"] = str(call.data["note"] or "").strip()[:MAX_NOTE_LEN]
        if meta.get("tags") or meta.get("note"):
            rules["contact_meta"][uid] = {"tags": meta.get("tags") or [], "note": meta.get("note") or ""}
        else:
            rules["contact_meta"].pop(uid, None)
        await rt.defaults.async_set(**{RULES_KEY: rules})
        out = rules["contact_meta"].get(uid) or {"tags": [], "note": ""}
        return {"thread_id": uid, "tags": out["tags"], "note": out["note"]}

    async def get_default_devices(call: ServiceCall) -> ServiceResponse:
        rt = _entry(hass, call).runtime_data
        devices = rt.bridge.devices_for()
        return {"devices": {k: ("***" if k == "mic_url" and v else v) for k, v in devices.items()}, "call_volume": rt.bridge.volume_for(None)}

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

    async def send_media_group(call: ServiceCall) -> ServiceResponse:
        """Nhiều ảnh/video MỘT lệnh (cùng dạng ``telegram_bot.send_media_group``). 2-10 tệp cùng nhóm (ảnh+video | tệp | âm thanh) và cùng định dạng chữ thì gửi thành MỘT album thật,
        mỗi tệp có caption riêng; còn lại (tệp lạ nhóm, tin thoại, định dạng khác nhau, 1 hoặc hơn 10 tệp) thì gửi lần lượt từng tệp, đúng thứ tự (tệp lỗi thì dừng và báo lỗi)."""
        async def go(rt):
            chat = await chat_of(rt, call.data)
            items = []
            for item in call.data["media"]:
                name, data = await _read_source(hass, item["file"])
                fmt = _PARSE_MODES.get(str(item.get("parse_mode") or "").lower(), call.data["format"])
                items.append((item["media_type"], name, data, item["caption"], fmt))
            reply = call.data.get("reply_to", 0)
            groups = {_ALBUM_GROUP.get(k) for k, _n, _d, _c, _f in items}
            if 2 <= len(items) <= 10 and None not in groups and len(groups) == 1 and len({f for *_x, f in items}) == 1:
                res = await rt.api.send_album(chat, [(k, n, d, c) for k, n, d, c, _f in items], reply, items[0][4])
                sent = [_msg(m) for m in res.get("messages") or []]
                return {"messages": sent, "count": len(sent), "album": True}
            sent = [_msg(await rt.api.send_file(chat, k, n, d, c, reply, f)) for k, n, d, c, f in items]
            return {"messages": sent, "count": len(sent), "album": False}
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

    def _catalog_service(name: str, spec: catalog.S):
        async def handler(call: ServiceCall) -> ServiceResponse:
            payload = {k: v for k, v in call.data.items() if k not in ("config_entry_id", "account", "peer", "target", "message_thread_id", "parse_mode", "format")}

            async def go(rt):
                if spec.target == "chat":
                    payload["chat_id"] = await chat_of(rt, call.data)
                elif spec.target == "user" and "user_id" not in payload:
                    payload["user_id"] = await rt.api.resolve(call.data["peer"])      # cuộc trò chuyện riêng có chat_id = user_id
                return await rt.api.do(name, payload)
            return await run(call, go)
        return handler

    for name, spec in catalog.SERVICES.items():
        hass.services.async_register(DOMAIN, name, _catalog_service(name, spec), schema=_with_recipient(hass, _catalog_schema(spec), spec.fill),
                                     supports_response=SupportsResponse.ONLY if spec.response == "only" else SupportsResponse.OPTIONAL)

    R = SupportsResponse.OPTIONAL
    for name, fn, schema, resp in (("make_call", make_call, MAKE_CALL, R),
                                   ("answer_call", answer_call, ANSWER_CALL, R),
                                   ("hangup_call", hangup_call, CALL_ID, R),
                                   ("end_call", hangup_call, CALL_ID, R),
                                   ("reject_call", reject_call, CALL_ONLY, R),
                                   ("take_over_call", take_over_call, CALL_ONLY, R),
                                   ("accept_video", accept_video, ACCEPT_VIDEO, R),
                                   ("say_in_call", say_in_call, SAY_IN_CALL, R),
                                   ("alert_call", alert_call, ALERT_CALL, R),
                                   ("start_call_mic", _start("mic"), CALL_ONLY, R),
                                   ("stop_call_mic", _mute("mic"), CALL_ONLY, R),
                                   ("start_call_video", _start("video"), CALL_ONLY, R),
                                   ("stop_call_video", _mute("video"), CALL_ONLY, R),
                                   ("set_rule", set_rule, SET_RULE, R),
                                   ("apply_call_rule", apply_call_rule, APPLY_RULE, R),
                                   ("remove_rule", remove_rule, REMOVE_RULE, R),
                                   ("list_rules", list_rules, NO_ARGS, SupportsResponse.ONLY),
                                   ("set_contact_tags", set_contact_tags, CONTACT_TAGS, R),
                                   ("get_default_devices", get_default_devices, NO_ARGS, SupportsResponse.ONLY),
                                   ("list_contacts", get_contacts, CONTACTS, SupportsResponse.ONLY),
                                   ("send_message", send_message, SEND, R),
                                   ("send_file", send_file, SEND_FILE, R),
                                   ("send_photo", fixed_kind("photo"), SEND_FILE, R),
                                   ("send_video", fixed_kind("video"), SEND_FILE, R),
                                   ("send_audio", fixed_kind("audio"), SEND_FILE, R),
                                   ("send_media_group", send_media_group, MEDIA_GROUP, R),
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
        hass.services.async_register(DOMAIN, name, fn, schema=_with_recipient(hass, schema), supports_response=resp)


async def _refresh(hass: HomeAssistant, call: ServiceCall) -> None:
    try:
        await _entry(hass, call).runtime_data.coordinator.async_request_refresh()
    except ServiceValidationError:
        pass
