"""Trò chuyện qua TDLib cho giao diện web: danh sách hội thoại, lịch sử, gửi tin, danh bạ, tệp, tìm kiếm,
cùng «bảng tin» (Feed) để trang nhận tin mới theo thời gian thực.

``ChatService`` bọc MỘT client TDLib (``client.call(request) -> dict``). Dữ liệu trả về đã được dịch sang
dạng gọn, ổn định cho giao diện: không lộ cấu trúc TDLib, không có mã bí mật.

Update của TDLib tới từ luồng nhận chung (``tdclient.TdPump``): ``handle_update`` chỉ cập nhật bộ nhớ đệm và
đẩy một dòng vào ``Feed`` (không gọi mạng, không chặn lâu), để không làm nghẽn luồng nhận.
"""

import collections
import threading
import time

PREVIEW_MAX = 120
FEED_MAX = 2000


# -- dịch dữ liệu (hàm thuần) -----------------------------------------------------------------------

def _user_name(u: dict) -> str:
    name = (u.get("first_name", "") + " " + u.get("last_name", "")).strip()
    if name:
        return name
    names = (u.get("usernames") or {}).get("active_usernames") or []
    return ("@" + names[0]) if names else str(u.get("id", ""))


def _file_info(f: dict | None) -> dict | None:
    """Tệp TDLib -> {id, size, path (nếu đã tải xong)}."""
    if not f:
        return None
    local = f.get("local") or {}
    done = bool(local.get("is_downloading_completed"))
    return {"id": f.get("id"), "size": f.get("size") or f.get("expected_size") or 0,
            "path": local.get("path") if done else ""}


def _best_photo_size(sizes: list[dict]) -> dict | None:
    """Kích thước ảnh vừa đủ để xem trong khung trò chuyện (lớn nhất không quá ~1280 px)."""
    best = None
    for s in sizes or []:
        if s.get("width", 0) <= 1280 and (best is None or s.get("width", 0) > best.get("width", 0)):
            best = s
    return best or (sizes[-1] if sizes else None)


def content_summary(c: dict) -> tuple[str, str]:
    """(loại, câu tóm tắt ngắn) của nội dung tin — dùng cho xem trước ở danh sách hội thoại."""
    t = c.get("@type", "")
    cap = ((c.get("caption") or {}).get("text") or "").strip()
    table = {
        "messageText": ("text", ((c.get("text") or {}).get("text") or "")),
        "messagePhoto": ("photo", "🖼 Ảnh" + (": " + cap if cap else "")),
        "messageVideo": ("video", "🎬 Video" + (": " + cap if cap else "")),
        "messageAnimation": ("animation", "GIF"),
        "messageVoiceNote": ("voice", "🎤 Tin nhắn thoại"),
        "messageVideoNote": ("video_note", "⏺ Video tròn"),
        "messageAudio": ("audio", "🎵 " + (((c.get("audio") or {}).get("title")) or "Âm thanh")),
        "messageDocument": ("document", "📎 " + (((c.get("document") or {}).get("file_name")) or "Tệp")),
        "messageSticker": ("sticker", ((c.get("sticker") or {}).get("emoji") or "") + " Nhãn dán"),
        "messageCall": ("call", "📞 Cuộc gọi"),
        "messagePoll": ("poll", "📊 Bình chọn"),
        "messageLocation": ("location", "📍 Vị trí"),
        "messageContact": ("contact", "👤 Danh bạ"),
    }
    kind, text = table.get(t, ("other", "[" + t.replace("message", "", 1).lower() + "]" if t else ""))
    return kind, text.strip()


def normalize_message(m: dict, sender_name: str = "") -> dict:
    c = m.get("content") or {}
    kind, summary = content_summary(c)
    out = {
        "id": m.get("id"), "chat_id": m.get("chat_id"), "date": m.get("date", 0),
        "edit_date": m.get("edit_date", 0), "outgoing": bool(m.get("is_outgoing")),
        "sender": sender_name, "kind": kind,
        "text": ((c.get("text") or {}).get("text") if kind == "text" else
                 (c.get("caption") or {}).get("text", "")) or "",
        "summary": summary,
        "reply_to": ((m.get("reply_to") or {}).get("message_id") or 0),
        "reactions": [{"emoji": ((r.get("type") or {}).get("emoji") or "?"),
                       "count": r.get("total_count", 0), "mine": bool(r.get("is_chosen"))}
                      for r in (((m.get("interaction_info") or {}).get("reactions") or {}).get("reactions") or [])],
        "file": None,
    }
    if kind == "photo":
        sz = _best_photo_size((c.get("photo") or {}).get("sizes") or [])
        if sz:
            out["file"] = dict(_file_info(sz.get("photo")) or {}, width=sz.get("width"), height=sz.get("height"),
                               mime="image/jpeg")
    elif kind in ("video", "animation", "audio", "document", "voice", "video_note", "sticker"):
        obj = {"video": c.get("video"), "animation": c.get("animation"), "audio": c.get("audio"),
               "document": c.get("document"), "voice": c.get("voice_note"), "video_note": c.get("video_note"),
               "sticker": c.get("sticker")}[kind] or {}
        f = obj.get("video") or obj.get("animation") or obj.get("audio") or obj.get("document") \
            or obj.get("voice") or obj.get("video_note") or obj.get("sticker")
        info = _file_info(f)
        if info:
            out["file"] = dict(info, name=obj.get("file_name", ""), mime=obj.get("mime_type", ""),
                               duration=obj.get("duration", 0), width=obj.get("width", 0),
                               height=obj.get("height", 0))
    return out


def chat_kind(ct: dict) -> str:
    t = ct.get("@type", "")
    if t == "chatTypePrivate":
        return "private"
    if t == "chatTypeSecret":
        return "secret"
    if t == "chatTypeBasicGroup":
        return "group"
    if t == "chatTypeSupergroup":
        return "channel" if ct.get("is_channel") else "group"
    return "other"


class Feed:
    """Bảng tin trong bộ nhớ (vòng tròn) để trang hỏi «có gì mới từ cursor». Không ghi đĩa."""

    def __init__(self, maxlen: int = FEED_MAX):
        self._cv = threading.Condition()
        self._items: collections.deque = collections.deque(maxlen=maxlen)
        self._cursor = 0

    def push(self, kind: str, **data) -> int:
        with self._cv:
            self._cursor += 1
            self._items.append({"id": self._cursor, "type": kind, "ts": time.time(), **data})
            self._cv.notify_all()
            return self._cursor

    def since(self, cursor: int, wait: float = 0.0) -> dict:
        """Các dòng có id > cursor. Chưa có thì chờ tối đa ``wait`` giây (long-poll)."""
        end = time.time() + wait
        with self._cv:
            while True:
                items = [i for i in self._items if i["id"] > cursor]
                if items or wait <= 0 or time.time() >= end:
                    # Cursor cũ hơn dòng đầu còn giữ: báo để trang tải lại toàn bộ.
                    first = self._items[0]["id"] if self._items else self._cursor + 1
                    return {"items": items, "cursor": self._cursor, "reset": cursor + 1 < first and cursor > 0}
                self._cv.wait(max(0.0, end - time.time()))


class ChatService:
    def __init__(self, client, feed: Feed | None = None, account: str = ""):
        self.client = client
        self.feed = feed or Feed()
        self.account = account
        self._users: dict[int, dict] = {}
        self._lock = threading.Lock()
        self.me_id: int | None = None

    # -- người dùng ---------------------------------------------------
    def user(self, user_id: int) -> dict:
        with self._lock:
            u = self._users.get(user_id)
        if u is None:
            u = self.client.call({"@type": "getUser", "user_id": int(user_id)})
            with self._lock:
                self._users[user_id] = u
        return u

    def sender_name(self, m: dict) -> str:
        s = m.get("sender_id") or {}
        if s.get("@type") == "messageSenderUser":
            try:
                return _user_name(self.user(s["user_id"]))
            except Exception:  # noqa: BLE001 - tên không lấy được thì để trống, không làm hỏng danh sách
                return ""
        if s.get("@type") == "messageSenderChat":
            return self.chat_title(s.get("chat_id"))
        return ""

    def chat_title(self, chat_id) -> str:
        try:
            return self.client.call({"@type": "getChat", "chat_id": int(chat_id)}).get("title", "")
        except Exception:  # noqa: BLE001
            return ""

    def me(self) -> dict:
        m = self.client.call({"@type": "getMe"})
        self.me_id = m.get("id")
        with self._lock:
            self._users[m["id"]] = m
        return {"id": m.get("id"), "name": _user_name(m), "phone": m.get("phone_number", "")}

    # -- hội thoại ----------------------------------------------------
    def summarize_chat(self, chat: dict) -> dict:
        lm = chat.get("last_message") or {}
        kind, summary = content_summary(lm.get("content") or {}) if lm else ("", "")
        pos = (chat.get("positions") or [{}])[0]
        photo = (chat.get("photo") or {}).get("small") or {}
        return {"id": chat.get("id"), "title": chat.get("title", ""), "kind": chat_kind(chat.get("type") or {}),
                "unread": chat.get("unread_count", 0), "unread_mentions": chat.get("unread_mention_count", 0),
                "pinned": bool(pos.get("is_pinned")),
                "muted": ((chat.get("notification_settings") or {}).get("mute_for", 0) or 0) > 0,
                "last": {"date": lm.get("date", 0), "outgoing": bool(lm.get("is_outgoing")),
                         "text": summary[:PREVIEW_MAX], "kind": kind} if lm else None,
                "avatar_file": photo.get("id"), "user_id": (chat.get("type") or {}).get("user_id")}

    def chats(self, limit: int = 60) -> list[dict]:
        try:
            self.client.call({"@type": "loadChats", "chat_list": {"@type": "chatListMain"}, "limit": limit}, timeout=15)
        except Exception:  # noqa: BLE001 - 404 = đã tải hết; lỗi khác thì vẫn trả những gì có sẵn
            pass
        ids = self.client.call({"@type": "getChats", "chat_list": {"@type": "chatListMain"}, "limit": limit}).get("chat_ids") or []
        out = []
        for cid in ids:
            try:
                out.append(self.summarize_chat(self.client.call({"@type": "getChat", "chat_id": cid})))
            except Exception:  # noqa: BLE001
                continue
        return out

    def history(self, chat_id: int, before: int = 0, limit: int = 30) -> list[dict]:
        """Tin cũ hơn ``before`` (0 = mới nhất), trả theo thứ tự thời gian tăng dần."""
        got: list[dict] = []
        frm = before
        for _ in range(3):                       # lần đầu TDLib có thể trả ít hơn limit: gọi thêm tối đa 2 lần
            r = self.client.call({"@type": "getChatHistory", "chat_id": int(chat_id), "from_message_id": frm,
                                  "offset": 0, "limit": max(1, limit - len(got)), "only_local": False}, timeout=30)
            msgs = r.get("messages") or []
            if not msgs:
                break
            got.extend(msgs)
            frm = msgs[-1]["id"]
            if len(got) >= limit:
                break
        out = [normalize_message(m, self.sender_name(m)) for m in got]
        out.sort(key=lambda x: x["id"])
        return out

    def mark_read(self, chat_id: int, message_ids: list[int]) -> None:
        if message_ids:
            self.client.call({"@type": "viewMessages", "chat_id": int(chat_id), "message_ids": [int(i) for i in message_ids],
                              "source": {"@type": "messageSourceChatHistory"}, "force_read": True})

    # -- gửi ----------------------------------------------------------
    def send_text(self, chat_id: int, text: str, reply_to: int = 0) -> dict:
        req = {"@type": "sendMessage", "chat_id": int(chat_id),
               "input_message_content": {"@type": "inputMessageText",
                                         "text": {"@type": "formattedText", "text": text, "entities": []},
                                         "link_preview_options": {"@type": "linkPreviewOptions", "is_disabled": False}}}
        if reply_to:
            req["reply_to"] = {"@type": "inputMessageReplyToMessage", "message_id": int(reply_to)}
        return normalize_message(self.client.call(req), "")

    def send_file(self, chat_id: int, path: str, kind: str, caption: str = "") -> dict:
        """kind: photo | video | document | voice | audio."""
        cap = {"@type": "formattedText", "text": caption, "entities": []}
        f = {"@type": "inputFileLocal", "path": path}
        # TDLib >= 1.8.x bọc tệp trong lớp riêng (inputPhoto/inputVideo/...): đưa thẳng inputFileLocal vào
        # thì lỗi "InputFile is not specified" (đã gặp thật).
        content = {
            "photo": {"@type": "inputMessagePhoto", "caption": cap,
                      "photo": {"@type": "inputPhoto", "photo": f, "width": 0, "height": 0, "added_sticker_file_ids": []}},
            "video": {"@type": "inputMessageVideo", "caption": cap,
                      "video": {"@type": "inputVideo", "video": f, "supports_streaming": True}},
            "voice": {"@type": "inputMessageVoiceNote", "caption": cap,
                      "voice_note": {"@type": "inputVoiceNote", "voice_note": f, "duration": 0, "waveform": ""}},
            "audio": {"@type": "inputMessageAudio", "caption": cap,
                      "audio": {"@type": "inputAudio", "audio": f}},
        }.get(kind) or {"@type": "inputMessageDocument", "caption": cap,
                        "document": {"@type": "inputDocument", "document": f}}
        return normalize_message(self.client.call({"@type": "sendMessage", "chat_id": int(chat_id),
                                                   "input_message_content": content}, timeout=60), "")

    def edit_text(self, chat_id: int, message_id: int, text: str) -> dict:
        return normalize_message(self.client.call({
            "@type": "editMessageText", "chat_id": int(chat_id), "message_id": int(message_id),
            "input_message_content": {"@type": "inputMessageText",
                                      "text": {"@type": "formattedText", "text": text, "entities": []}}}), "")

    def delete(self, chat_id: int, message_ids: list[int], revoke: bool = True) -> None:
        self.client.call({"@type": "deleteMessages", "chat_id": int(chat_id),
                          "message_ids": [int(i) for i in message_ids], "revoke": bool(revoke)})

    def forward(self, to_chat_id: int, from_chat_id: int, message_ids: list[int]) -> None:
        self.client.call({"@type": "forwardMessages", "chat_id": int(to_chat_id), "from_chat_id": int(from_chat_id),
                          "message_ids": [int(i) for i in message_ids]})

    def react(self, chat_id: int, message_id: int, emoji: str, on: bool = True) -> None:
        typ = {"@type": "reactionTypeEmoji", "emoji": emoji}
        self.client.call({"@type": "addMessageReaction" if on else "removeMessageReaction",
                          "chat_id": int(chat_id), "message_id": int(message_id), "reaction_type": typ,
                          **({"is_big": False, "update_recent_reactions": True} if on else {})})

    # -- danh bạ, tìm kiếm --------------------------------------------
    def contacts(self) -> list[dict]:
        ids = self.client.call({"@type": "getContacts"}).get("user_ids") or []
        out = []
        for uid in ids:
            try:
                u = self.user(uid)
                out.append({"id": uid, "name": _user_name(u), "phone": u.get("phone_number", ""),
                            "username": ((u.get("usernames") or {}).get("active_usernames") or [""])[0]})
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda x: x["name"].lower())
        return out

    def open_private(self, user_id: int) -> int:
        return int(self.client.call({"@type": "createPrivateChat", "user_id": int(user_id), "force": False})["id"])

    def search(self, query: str, limit: int = 20) -> list[dict]:
        ids = self.client.call({"@type": "searchChats", "query": query, "limit": limit}).get("chat_ids") or []
        out = []
        for cid in ids:
            try:
                out.append(self.summarize_chat(self.client.call({"@type": "getChat", "chat_id": cid})))
            except Exception:  # noqa: BLE001
                continue
        return out

    # -- tệp ----------------------------------------------------------
    def download(self, file_id: int, timeout: float = 60) -> str:
        """Tải tệp (nếu chưa có) và trả đường dẫn cục bộ; rỗng nếu chưa xong."""
        f = self.client.call({"@type": "downloadFile", "file_id": int(file_id), "priority": 16, "offset": 0,
                              "limit": 0, "synchronous": True}, timeout=timeout)
        local = f.get("local") or {}
        return local.get("path", "") if local.get("is_downloading_completed") else ""

    # -- update từ TDLib (gọi từ luồng nhận chung: nhanh, không mạng) ---
    def handle_update(self, obj: dict) -> None:
        t = obj.get("@type")
        if t == "updateNewMessage":
            m = obj.get("message") or {}
            self.feed.push("message", chat_id=m.get("chat_id"), message=normalize_message(m, self._cached_sender(m)))
        elif t == "updateMessageSendSucceeded":
            # sendMessage trả tin TẠM (id tạm); gửi xong TDLib đổi sang id thật bằng update này.
            m = obj.get("message") or {}
            self.feed.push("send_ok", chat_id=m.get("chat_id"), old_id=obj.get("old_message_id"),
                           message=normalize_message(m, self._cached_sender(m)))
        elif t == "updateMessageSendFailed":
            m = obj.get("message") or {}
            self.feed.push("send_failed", chat_id=m.get("chat_id"), old_id=obj.get("old_message_id"),
                           error=(obj.get("error") or {}).get("message", "gửi không được"))
        elif t == "updateMessageContent":
            self.feed.push("message_changed", chat_id=obj.get("chat_id"), message_id=obj.get("message_id"))
        elif t == "updateMessageEdited":
            self.feed.push("message_changed", chat_id=obj.get("chat_id"), message_id=obj.get("message_id"))
        elif t == "updateDeleteMessages" and obj.get("is_permanent"):
            self.feed.push("deleted", chat_id=obj.get("chat_id"), ids=obj.get("message_ids") or [])
        elif t in ("updateChatLastMessage", "updateChatReadInbox", "updateChatTitle", "updateChatPhoto",
                   "updateChatPosition", "updateNewChat", "updateChatNotificationSettings",
                   "updateChatUnreadMentionCount"):
            cid = obj.get("chat_id") or (obj.get("chat") or {}).get("id")
            if cid:
                self.feed.push("chat", chat_id=cid)
        elif t == "updateUser":
            u = obj.get("user") or {}
            if u.get("id"):
                with self._lock:
                    self._users[u["id"]] = u
        elif t == "updateChatAction":
            self.feed.push("typing", chat_id=obj.get("chat_id"),
                           action=(obj.get("action") or {}).get("@type", ""))

    def _cached_sender(self, m: dict) -> str:
        s = m.get("sender_id") or {}
        if s.get("@type") == "messageSenderUser":
            with self._lock:
                u = self._users.get(s.get("user_id"))
            return _user_name(u) if u else ""
        return ""
