"""Dịch vụ Telegram theo DANH MỤC (hội thoại, tin nhắn, danh bạ, nhóm, tài khoản) — cùng ý với catalog dịch vụ của zalo_call.

Mỗi dịch vụ là một hàm ``(client, args) -> dict`` dựng yêu cầu TDLib ĐÚNG lược đồ ``td_api.tl`` (test kiểm từng yêu cầu bằng ``tests/schema.py``) và trả kết quả gọn,
không lộ cấu trúc TDLib. Chỉ các tên trong ``HANDLERS`` mới chạy được (danh sách cho phép): không có đường gọi hàm TDLib tuỳ ý. Lỗi đầu vào → ``ValueError`` (route đổi thành 400).
"""
from __future__ import annotations

import time
from typing import Any, Callable

from .chat import _user_name


def _ft(text: Any) -> dict:
    return {"@type": "formattedText", "text": str(text or ""), "entities": []}


def _chat_list(archive: bool) -> dict:
    return {"@type": "chatListArchive"} if archive else {"@type": "chatListMain"}


def _sender(user_id: Any) -> dict:
    return {"@type": "messageSenderUser", "user_id": _int(user_id, "user_id")}


def _int(value: Any, name: str, low: int | None = None, high: int | None = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s phải là số nguyên" % name)
    if (low is not None and n < low) or (high is not None and n > high):
        raise ValueError("%s phải trong khoảng %s..%s" % (name, low, high))
    return n


def _ids(value: Any, name: str) -> list[int]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).replace(";", ",").split(",")
    return [_int(str(v).strip(), name) for v in items if str(v).strip()]


def _user_brief(u: dict) -> dict:
    names = (u.get("usernames") or {}).get("active_usernames") or []
    out = {"user_id": u.get("id"), "name": _user_name(u), "username": names[0] if names else "", "is_bot": bool((u.get("type") or {}).get("@type") == "userTypeBot")}
    if u.get("phone_number"):
        out["phone"] = "+" + str(u["phone_number"]).lstrip("+")
    status = (u.get("status") or {}).get("@type", "").replace("userStatus", "")
    if status:
        out["status"] = status.lower()
    return out


# -- hội thoại ---------------------------------------------------------------------------------------------

def set_mute(c: Any, a: dict) -> dict:
    """mute_for (giây): 0 = bật lại thông báo, -1 = theo mặc định, số dương = tắt chừng đó giây (tối đa ~ 1 năm)."""
    chat_id = _int(a.get("chat_id"), "chat_id")
    mute_for = _int(a.get("mute_for", 0), "mute_for", -1, 366 * 24 * 3600)
    cur = c.call({"@type": "getChat", "chat_id": chat_id}).get("notification_settings") or {}
    new = dict(cur)
    new["@type"] = "chatNotificationSettings"
    new["use_default_mute_for"] = mute_for < 0
    new["mute_for"] = max(0, mute_for)
    c.call({"@type": "setChatNotificationSettings", "chat_id": chat_id, "notification_settings": new})
    return {"chat_id": chat_id, "mute_for": mute_for}


def pin_chat(c: Any, a: dict) -> dict:
    chat_id, pinned = _int(a.get("chat_id"), "chat_id"), bool(a.get("pinned", True))
    c.call({"@type": "toggleChatIsPinned", "chat_list": _chat_list(bool(a.get("archived", False))), "chat_id": chat_id, "is_pinned": pinned})
    return {"chat_id": chat_id, "pinned": pinned}


def archive_chat(c: Any, a: dict) -> dict:
    chat_id, archive = _int(a.get("chat_id"), "chat_id"), bool(a.get("archive", True))
    c.call({"@type": "addChatToList", "chat_id": chat_id, "chat_list": _chat_list(archive)})
    return {"chat_id": chat_id, "archived": archive}


def mark_chat_unread(c: Any, a: dict) -> dict:
    chat_id, unread = _int(a.get("chat_id"), "chat_id"), bool(a.get("unread", True))
    c.call({"@type": "toggleChatIsMarkedAsUnread", "chat_id": chat_id, "is_marked_as_unread": unread})
    return {"chat_id": chat_id, "unread": unread}


def set_auto_delete(c: Any, a: dict) -> dict:
    """seconds: 0 = tắt tự xoá tin nhắn; thường dùng 86400 (1 ngày), 604800 (1 tuần), 2678400 (1 tháng)."""
    chat_id, secs = _int(a.get("chat_id"), "chat_id"), _int(a.get("seconds", 0), "seconds", 0, 366 * 24 * 3600)
    c.call({"@type": "setChatMessageAutoDeleteTime", "chat_id": chat_id, "message_auto_delete_time": secs})
    return {"chat_id": chat_id, "seconds": secs}


_ACTIONS = {"typing": "chatActionTyping", "recording_voice": "chatActionRecordingVoiceNote", "cancel": "chatActionCancel"}


def send_typing(c: Any, a: dict) -> dict:
    chat_id, act = _int(a.get("chat_id"), "chat_id"), str(a.get("action") or "typing")
    if act not in _ACTIONS:
        raise ValueError("action chỉ gồm: %s" % ", ".join(_ACTIONS))
    c.call({"@type": "sendChatAction", "chat_id": chat_id, "business_connection_id": "", "action": {"@type": _ACTIONS[act]}})
    return {"chat_id": chat_id, "action": act}


def delete_chat_history(c: Any, a: dict) -> dict:
    """KHÔNG hoàn tác được. revoke = xoá luôn ở phía người kia (nếu Telegram cho phép)."""
    chat_id = _int(a.get("chat_id"), "chat_id")
    c.call({"@type": "deleteChatHistory", "chat_id": chat_id, "remove_from_chat_list": bool(a.get("remove_from_list", False)), "revoke": bool(a.get("revoke", False))})
    return {"chat_id": chat_id, "deleted": True}


def rename_chat(c: Any, a: dict) -> dict:
    chat_id, title = _int(a.get("chat_id"), "chat_id"), str(a.get("title") or "").strip()
    if not title or len(title) > 128:
        raise ValueError("title phải 1-128 ký tự")
    c.call({"@type": "setChatTitle", "chat_id": chat_id, "title": title})
    return {"chat_id": chat_id, "title": title}


def leave_chat(c: Any, a: dict) -> dict:
    chat_id = _int(a.get("chat_id"), "chat_id")
    c.call({"@type": "leaveChat", "chat_id": chat_id})
    return {"chat_id": chat_id, "left": True}


def get_chat_info(c: Any, a: dict) -> dict:
    chat_id = _int(a.get("chat_id"), "chat_id")
    ch = c.call({"@type": "getChat", "chat_id": chat_id})
    t = (ch.get("type") or {}).get("@type", "").replace("chatType", "")
    ns = ch.get("notification_settings") or {}
    return {"chat_id": ch.get("id", chat_id), "title": ch.get("title", ""), "kind": t.lower(), "unread": ch.get("unread_count", 0),
            "muted_for": ns.get("mute_for", 0), "auto_delete_seconds": ch.get("message_auto_delete_time", 0), "has_protected_content": bool(ch.get("has_protected_content"))}


# -- tin nhắn ----------------------------------------------------------------------------------------------

def pin_message(c: Any, a: dict) -> dict:
    chat_id, mid = _int(a.get("chat_id"), "chat_id"), _int(a.get("message_id"), "message_id", 1)
    c.call({"@type": "pinChatMessage", "chat_id": chat_id, "message_id": mid, "disable_notification": not bool(a.get("notify", False)),
            "only_for_self": bool(a.get("only_for_self", False))})
    return {"chat_id": chat_id, "message_id": mid, "pinned": True}


def unpin_message(c: Any, a: dict) -> dict:
    chat_id, mid = _int(a.get("chat_id"), "chat_id"), _int(a.get("message_id"), "message_id", 1)
    c.call({"@type": "unpinChatMessage", "chat_id": chat_id, "message_id": mid})
    return {"chat_id": chat_id, "message_id": mid, "pinned": False}


def send_poll(c: Any, a: dict) -> dict:
    """Bình chọn: 2-10 lựa chọn; quiz_answer = số thứ tự (từ 1) của đáp án đúng thì thành câu đố (một đáp án)."""
    chat_id, q = _int(a.get("chat_id"), "chat_id"), str(a.get("question") or "").strip()
    opts = a.get("options")
    opts = [s.strip() for s in (opts if isinstance(opts, list) else str(opts or "").split("\n")) if str(s).strip()]
    if not q or len(q) > 300:
        raise ValueError("question phải 1-300 ký tự")
    if not 2 <= len(opts) <= 10:
        raise ValueError("options cần 2-10 lựa chọn")
    quiz = a.get("quiz_answer")
    if quiz not in (None, "", 0):
        n = _int(quiz, "quiz_answer", 1, len(opts))
        ptype = {"@type": "inputPollTypeQuiz", "correct_option_ids": [n - 1], "explanation": _ft(a.get("explanation"))}
        multiple = False
    else:
        ptype = {"@type": "inputPollTypeRegular", "allow_adding_options": False}
        multiple = bool(a.get("multiple", False))
    content = {"@type": "inputMessagePoll", "question": _ft(q), "options": [{"@type": "inputPollOption", "text": _ft(o)} for o in opts],
               "is_anonymous": bool(a.get("anonymous", True)), "allows_multiple_answers": multiple, "type": ptype}
    req = {"@type": "sendMessage", "chat_id": chat_id, "input_message_content": content}
    m = c.call(req, timeout=30)
    return {"chat_id": chat_id, "message_id": m.get("id"), "options": len(opts)}


def vote_poll(c: Any, a: dict) -> dict:
    chat_id, mid = _int(a.get("chat_id"), "chat_id"), _int(a.get("message_id"), "message_id", 1)
    ids = [i - 1 for i in _ids(a.get("options"), "options")]
    if any(i < 0 for i in ids):
        raise ValueError("options đánh số từ 1")
    c.call({"@type": "setPollAnswer", "chat_id": chat_id, "message_id": mid, "option_ids": ids})
    return {"chat_id": chat_id, "message_id": mid, "voted": [i + 1 for i in ids]}


def stop_poll(c: Any, a: dict) -> dict:
    chat_id, mid = _int(a.get("chat_id"), "chat_id"), _int(a.get("message_id"), "message_id", 1)
    c.call({"@type": "stopPoll", "chat_id": chat_id, "message_id": mid})
    return {"chat_id": chat_id, "message_id": mid, "closed": True}


# -- người dùng / danh bạ ----------------------------------------------------------------------------------

def find_user(c: Any, a: dict) -> dict:
    """Tìm người theo số điện thoại (có mã nước) hoặc @username công khai."""
    phone, uname = str(a.get("phone") or "").strip(), str(a.get("username") or "").strip().lstrip("@")
    if bool(phone) == bool(uname):
        raise ValueError("nêu đúng MỘT trong phone hoặc username")
    if phone:
        u = c.call({"@type": "searchUserByPhoneNumber", "phone_number": phone.lstrip("+").replace(" ", ""), "only_local": False})
        return _user_brief(u)
    ch = c.call({"@type": "searchPublicChat", "username": uname})
    if (ch.get("type") or {}).get("@type") != "chatTypePrivate":
        return {"user_id": None, "name": ch.get("title", ""), "username": uname, "kind": (ch.get("type") or {}).get("@type", "").replace("chatType", "").lower(), "chat_id": ch.get("id")}
    return _user_brief(c.call({"@type": "getUser", "user_id": (ch["type"]).get("user_id")}))


def get_user_info(c: Any, a: dict) -> dict:
    return _user_brief(c.call({"@type": "getUser", "user_id": _int(a.get("user_id"), "user_id", 1)}))


def add_contact(c: Any, a: dict) -> dict:
    uid, first = _int(a.get("user_id"), "user_id", 1), str(a.get("first_name") or "").strip()
    if not first:
        raise ValueError("first_name không được trống")
    c.call({"@type": "addContact", "user_id": uid, "share_phone_number": bool(a.get("share_phone", False)),
            "contact": {"@type": "importedContact", "phone_number": str(a.get("phone") or ""), "first_name": first, "last_name": str(a.get("last_name") or ""), "note": _ft("")}})
    return {"user_id": uid, "added": True}


def remove_contact(c: Any, a: dict) -> dict:
    uid = _int(a.get("user_id"), "user_id", 1)
    c.call({"@type": "removeContacts", "user_ids": [uid]})
    return {"user_id": uid, "removed": True}


def block_user(c: Any, a: dict) -> dict:
    uid = _int(a.get("user_id"), "user_id", 1)
    c.call({"@type": "setMessageSenderBlockList", "sender_id": _sender(uid), "block_list": {"@type": "blockListMain"}})
    return {"user_id": uid, "blocked": True}


def unblock_user(c: Any, a: dict) -> dict:
    uid = _int(a.get("user_id"), "user_id", 1)
    c.call({"@type": "setMessageSenderBlockList", "sender_id": _sender(uid)})
    return {"user_id": uid, "blocked": False}


# -- nhóm --------------------------------------------------------------------------------------------------

def create_group(c: Any, a: dict) -> dict:
    title, users = str(a.get("title") or "").strip(), _ids(a.get("user_ids"), "user_ids")
    if not title or len(title) > 128:
        raise ValueError("title phải 1-128 ký tự")
    if not users:
        raise ValueError("user_ids cần ít nhất một người")
    r = c.call({"@type": "createNewBasicGroupChat", "user_ids": users, "title": title, "message_auto_delete_time": 0})
    failed = (r.get("failed_to_add_members") or {}).get("failed_to_add_members") or []
    return {"chat_id": r.get("chat_id"), "title": title, "failed_user_ids": [f.get("user_id") for f in failed if f.get("user_id")]}


def add_chat_member(c: Any, a: dict) -> dict:
    chat_id, uid = _int(a.get("chat_id"), "chat_id"), _int(a.get("user_id"), "user_id", 1)
    r = c.call({"@type": "addChatMember", "chat_id": chat_id, "user_id": uid, "forward_limit": _int(a.get("forward_limit", 0), "forward_limit", 0, 100)})
    failed = (r or {}).get("failed_to_add_members") or []
    return {"chat_id": chat_id, "user_id": uid, "added": not failed}


def remove_chat_member(c: Any, a: dict) -> dict:
    """Mời ra khỏi nhóm: cấm ~1 phút rồi người đó có thể được mời lại (không phải cấm vĩnh viễn)."""
    chat_id, uid = _int(a.get("chat_id"), "chat_id"), _int(a.get("user_id"), "user_id", 1)
    c.call({"@type": "setChatMemberStatus", "chat_id": chat_id, "member_id": _sender(uid),
            "status": {"@type": "chatMemberStatusBanned", "banned_until_date": int(time.time()) + 60}})
    return {"chat_id": chat_id, "user_id": uid, "removed": True}


def create_invite_link(c: Any, a: dict) -> dict:
    chat_id = _int(a.get("chat_id"), "chat_id")
    exp = _int(a.get("expire_seconds", 0), "expire_seconds", 0)
    link = c.call({"@type": "createChatInviteLink", "chat_id": chat_id, "name": str(a.get("name") or ""), "expiration_date": int(time.time()) + exp if exp else 0,
                   "member_limit": _int(a.get("member_limit", 0), "member_limit", 0, 99999), "creates_join_request": bool(a.get("join_request", False))})
    return {"chat_id": chat_id, "invite_link": link.get("invite_link", "")}


def join_chat(c: Any, a: dict) -> dict:
    link = str(a.get("invite_link") or "").strip()
    if not link.startswith(("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/")):
        raise ValueError("invite_link phải là liên kết t.me")
    r = c.call({"@type": "joinChatByInviteLink", "invite_link": link})
    return {"joined": True, "chat_id": r.get("chat_id")}


# -- tài khoản ---------------------------------------------------------------------------------------------

def get_me(c: Any, a: dict) -> dict:
    return _user_brief(c.call({"@type": "getMe"}))


def set_name(c: Any, a: dict) -> dict:
    first, last = str(a.get("first_name") or "").strip(), str(a.get("last_name") or "").strip()
    if not first or len(first) > 64 or len(last) > 64:
        raise ValueError("first_name 1-64 ký tự, last_name tối đa 64")
    c.call({"@type": "setName", "first_name": first, "last_name": last})
    return {"first_name": first, "last_name": last}


def set_bio(c: Any, a: dict) -> dict:
    bio = str(a.get("bio") or "").strip()
    if len(bio) > 70:
        raise ValueError("bio tối đa 70 ký tự")
    c.call({"@type": "setBio", "bio": bio})
    return {"bio": bio}


def set_username(c: Any, a: dict) -> dict:
    name = str(a.get("username") or "").strip().lstrip("@")
    c.call({"@type": "setUsername", "username": name})
    return {"username": name}


HANDLERS: dict[str, Callable[[Any, dict], dict]] = {
    "set_mute": set_mute, "pin_chat": pin_chat, "archive_chat": archive_chat, "mark_chat_unread": mark_chat_unread, "set_auto_delete": set_auto_delete,
    "send_typing": send_typing, "delete_chat_history": delete_chat_history, "rename_chat": rename_chat, "leave_chat": leave_chat, "get_chat_info": get_chat_info,
    "pin_message": pin_message, "unpin_message": unpin_message, "send_poll": send_poll, "vote_poll": vote_poll, "stop_poll": stop_poll,
    "find_user": find_user, "get_user_info": get_user_info, "add_contact": add_contact, "remove_contact": remove_contact, "block_user": block_user, "unblock_user": unblock_user,
    "create_group": create_group, "add_chat_member": add_chat_member, "remove_chat_member": remove_chat_member, "create_invite_link": create_invite_link, "join_chat": join_chat,
    "get_me": get_me, "set_name": set_name, "set_bio": set_bio, "set_username": set_username,
}


def run_service(client: Any, name: str, args: dict) -> dict:
    """Chạy một dịch vụ trong danh sách cho phép; tên lạ → KeyError (route đổi thành 404)."""
    return HANDLERS[name](client, args if isinstance(args, dict) else {})
