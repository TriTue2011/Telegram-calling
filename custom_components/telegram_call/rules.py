"""Quy tắc xử lý cuộc gọi Telegram ĐẾN theo NGƯỜI — cùng mô hình với ``zalo_call`` (xem ``zalo_call/rules.py``). Riêng tư và bảo mật trước hết:

  * KHÔNG CÓ QUY TẮC = KHÔNG LÀM GÌ: cuộc gọi chỉ đổ chuông (không nghe, không báo loa).
  * Nguyên tắc: automation là chính — mỗi cuộc gọi đến kích hoạt automation, automation gọi ``telegram_call.apply_call_rule``
    (quy tắc chỉ sống đúng một cuộc gọi). ``set_rule`` lưu sẵn chỉ là bản dự phòng khi KHÔNG có automation nào áp quy tắc cho cuộc gọi đó.
  * Phạm vi (``scope``): ``thread`` (ID số của người dùng Telegram) > ``tag`` (thẻ gắn cho người) > ``contacts`` (người trong danh bạ Telegram) |
    ``strangers`` (người lạ). Quy tắc theo ID THAY HẲN quy tắc nhóm, nên ``answer: false`` ở một người chặn được quy tắc danh bạ dễ dãi hơn.
    Telegram Call chỉ có cuộc gọi 1-1 nên KHÔNG có phạm vi ``groups`` như Zalo.
  * Không biết người đó quen hay lạ (danh bạ chưa tải được): chỉ quy tắc theo ID/thẻ được áp, quy tắc contacts/strangers bị bỏ qua.
  * Thiết bị nhà do ``connect_now`` (nối NGAY khi nghe máy) và ``connect`` (nối khi người nhà bấm «nói trực tiếp») quyết, là các ô TÍCH: ``mic``,
    ``speaker``, ``camera``. Không tích = KHÔNG nối: nghe máy mà mic im lặng, cuộc gọi video thì gửi hình đen.
  * ``announce: true`` (cần ``announce_to``): có cuộc gọi đến thì loa trong nhà đọc thông báo. ``announce_repeat`` = số giây báo lại; ``announce_volume`` = âm lượng loa lúc báo.
  * ``greeting``: lời nhắn đọc VÀO cuộc gọi ngay khi nghe máy (giọng TTS của gateway); ``hangup_after: true`` = đọc xong thì cúp máy.
  * ``video_request``: người gọi BẬT camera giữa cuộc thoại (xin chuyển sang video): ``ask`` (mặc định: chỉ báo sự kiện ``video_request``, không tự đồng ý), ``accept``
    (đồng ý, không gửi hình trong nhà: hình đen), ``accept_camera`` (đồng ý và gửi camera đã chọn trong quy tắc / mặc định).
  * ``echo_guard``: chế độ chống vọng của quy tắc (``auto`` | ``on`` | ``duplex`` | ``off``); ``auto`` = theo mặc định của thiết bị.
  * KHÔNG có trợ lý AI trò chuyện: gateway Telegram chưa có đường nhận tiếng bên kia cho HA.
"""
from __future__ import annotations

from typing import Any

from .echo_policy import ECHO_MODES


class RuleError(ValueError):
    """Quy tắc/phạm vi không hợp lệ (dịch vụ đổi thành ServiceValidationError)."""


SCOPES = ("thread", "tag", "contacts", "strangers")
MAX_TAG_LEN = 40
MAX_TAGS_PER_CONTACT = 10
MAX_NOTE_LEN = 500
CALL_TYPES = ("voice", "video")
MAX_DELAY = 120
MAX_ANNOUNCE_REPEAT = 60
CONNECT_PARTS = ("mic", "speaker", "camera")
VIDEO_REQUEST = ("ask", "accept", "accept_camera")      # người gọi bật camera giữa cuộc thoại (xin chuyển sang video)
CONNECT = ("never", "voice", "always") + CONNECT_PARTS
RULES_KEY = "rules"

DEFAULT_RULE: dict[str, Any] = {
    "name": "", "answer": False, "answer_delay": 3, "call_types": ["voice", "video"],
    "pipeline": "", "greeting": "", "hangup_after": False,
    "announce": False, "announce_to": "", "announce_text": "", "announce_repeat": 0, "announce_volume": 0,
    "connect_now": [], "connect": [], "video_request": "ask", "echo_guard": "auto", "speaker": "", "mic": "", "mic_url": "", "camera": "", "volume": 0,
}
_STR = ("name", "pipeline", "greeting", "speaker", "mic", "mic_url", "camera", "announce_to", "announce_text")
_BOOL = ("answer", "announce", "hangup_after")


def parse_connect(value: Any) -> list[str]:
    """Danh sách ô đã tích (mic, speaker, camera theo thứ tự đó). Lối tắt: voice = mic+speaker, always = cả ba, never = không. Giá trị lạ → RuleError."""
    raw = [value] if isinstance(value, str) else list(value or [])
    parts: set[str] = set()
    for item in raw:
        v = str(item).strip().lower()
        if v == "voice":
            parts |= {"mic", "speaker"}
        elif v == "always":
            parts |= set(CONNECT_PARTS)
        elif v in CONNECT_PARTS:
            parts.add(v)
        elif v not in ("never", ""):
            raise RuleError("connect chỉ gồm: %s" % ", ".join(CONNECT))
    return [p for p in CONNECT_PARTS if p in parts]


def empty_rules() -> dict[str, Any]:
    # tags: quy tắc theo THẺ ({thẻ: quy tắc}); contact_meta: thẻ + ghi chú từng người ({user_id: {"tags": [...], "note": "..."}}).
    return {"threads": {}, "tags": {}, "contact_meta": {}, "contacts": {}, "strangers": {}}


def norm_tag(value: Any) -> str:
    return " ".join(str(value or "").replace(",", " ").split()).casefold()[:MAX_TAG_LEN]


def parse_tags(value: Any) -> list[str]:
    parts = value.split(",") if isinstance(value, str) else (value if isinstance(value, (list, tuple)) else [])
    out: list[str] = []
    for p in parts:
        t = norm_tag(p)
        if t and t not in out:
            out.append(t)
    return out[:MAX_TAGS_PER_CONTACT]


def get_rules(defaults: Any) -> dict[str, Any]:
    value = defaults.get(RULES_KEY)
    base = empty_rules()
    if isinstance(value, dict):
        for k in base:
            if isinstance(value.get(k), dict):
                base[k] = value[k]
    return base


def _int_field(data: dict[str, Any], rule: dict[str, Any], key: str, low: int, high: int, what: str) -> None:
    if data.get(key) is not None:
        try:
            rule[key] = max(low, min(high, int(data[key])))
        except (TypeError, ValueError) as err:
            raise RuleError("%s phải là số %d-%d" % (what, low, high)) from err


def normalize(data: dict[str, Any], old: dict[str, Any] | None = None) -> dict[str, Any]:
    """Quy tắc đã kiểm tra: trường nêu trong `data` ghi đè `old`; trường khác giữ như cũ (hoặc mặc định). Lỗi thì RuleError."""
    rule = {**DEFAULT_RULE, **(old or {})}
    for k in _STR:
        if k in data and data[k] is not None:
            rule[k] = str(data[k]).strip()
    for k in _BOOL:
        if k in data and data[k] is not None:
            rule[k] = bool(data[k])
    _int_field(data, rule, "answer_delay", 0, MAX_DELAY, "answer_delay")
    _int_field(data, rule, "announce_repeat", 0, MAX_ANNOUNCE_REPEAT, "announce_repeat")
    _int_field(data, rule, "announce_volume", 0, 100, "announce_volume")
    _int_field(data, rule, "volume", 0, 100, "volume")
    if data.get("video_request") is not None:
        if data["video_request"] not in VIDEO_REQUEST:
            raise RuleError("video_request chỉ gồm: %s" % ", ".join(VIDEO_REQUEST))
        rule["video_request"] = data["video_request"]
    if data.get("echo_guard") is not None:
        if data["echo_guard"] not in ECHO_MODES:
            raise RuleError("echo_guard chỉ gồm: %s" % ", ".join(ECHO_MODES))
        rule["echo_guard"] = data["echo_guard"]
    if data.get("call_types") is not None:
        types = [data["call_types"]] if isinstance(data["call_types"], str) else list(data["call_types"])
        if [t for t in types if t not in CALL_TYPES]:
            raise RuleError("call_types chỉ gồm: %s" % ", ".join(CALL_TYPES))
        rule["call_types"] = types
    for key in ("connect", "connect_now"):
        if data.get(key) is not None:
            rule[key] = parse_connect(data[key])
    if rule["announce"] and not rule["announce_to"]:
        raise RuleError("announce: true cần announce_to (loa media_player sẽ phát thông báo «có cuộc gọi»)")
    if rule["hangup_after"] and not rule["greeting"]:
        raise RuleError("hangup_after: true cần greeting (lời nhắn đọc xong thì cúp máy)")
    return rule


def validate_scope(scope: str, thread_id: Any, tag: Any = "") -> str:
    """Khoá của phạm vi: ID người dùng khi scope = thread, thẻ chuẩn hoá khi scope = tag, "" với các phạm vi còn lại."""
    if scope not in SCOPES:
        raise RuleError("scope phải là một trong: %s" % ", ".join(SCOPES))
    if scope == "thread":
        tid = str(thread_id or "").strip()
        if not tid.isdigit():
            raise RuleError("scope «thread» cần thread_id là ID số của người dùng Telegram")
        return tid
    if scope == "tag":
        key = norm_tag(tag)
        if not key:
            raise RuleError("scope «tag» cần tag (tên thẻ gắn cho người trong danh bạ)")
        return key
    return ""


def resolve(rules: dict[str, Any], user_id: Any, contact_status: str | None) -> dict[str, Any] | None:
    """Quy tắc lưu sẵn áp cho một người, hoặc None (= không làm gì). contact_status: friend / stranger / unknown / None."""
    tid = str(user_id or "")
    rule = rules["threads"].get(tid)
    if rule is not None:
        return rule
    for t in (((rules.get("contact_meta") or {}).get(tid) or {}).get("tags") or []):
        tagged = (rules.get("tags") or {}).get(t)
        if tagged:
            return tagged
    if contact_status == "friend":
        return rules["contacts"] or None
    if contact_status == "stranger":
        return rules["strangers"] or None
    return None
