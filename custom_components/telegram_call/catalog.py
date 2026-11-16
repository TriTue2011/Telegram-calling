"""Danh mục dịch vụ Telegram theo bảng (hội thoại, tin nhắn, danh bạ, nhóm, tài khoản) — cùng ý với catalog dịch vụ của zalo_call.

MỘT bảng ``SERVICES`` là nguồn duy nhất cho ba nơi: đăng ký dịch vụ lúc chạy (``services.py``), ``services.yaml`` và bản dịch (``tools/gen_catalog.py`` sinh ra). Mỗi dịch vụ gọi một
hàm trong ``telegram-gateway/gateway/td_services.py`` qua ``POST /api/do/<tên>``; gateway dựng yêu cầu TDLib đúng ``td_api.tl`` (xem ``tests``).

``target``: ``chat`` = nhắm một cuộc trò chuyện (``chat_id`` / ``peer`` / ``recipient``); ``user`` = nhắm một người (``user_id`` / ``peer`` / ``recipient``); ``none`` = không nhắm ai.
"""
from __future__ import annotations

from typing import Any, NamedTuple


class F(NamedTuple):
    key: str
    kind: str                  # int | bool | text | long | list_int | list_text | select
    required: bool
    default: Any
    vi_name: str
    vi_desc: str
    en_name: str
    en_desc: str
    lo: int | None = None      # int: giá trị nhỏ nhất
    hi: int | None = None      # int: giá trị lớn nhất
    options: tuple[str, ...] = ()


class S(NamedTuple):
    target: str
    response: str              # "only" = chỉ đọc, trả dữ liệu | "optional"
    vi_name: str
    vi_desc: str
    en_name: str
    en_desc: str
    fields: tuple[F, ...] = ()
    fill: tuple[str, ...] | None = None    # ô mà ``recipient`` điền; None = theo mặc định của target


def _chat_id(desc_vi="", desc_en="") -> tuple:
    return ()


MUTE = F("mute_for", "int", False, 0, "Tắt thông báo trong (giây)", "0 = bật lại thông báo; -1 = theo mặc định của Telegram; số dương = tắt chừng đó giây (tối đa 1 năm: 31622400). Ví dụ 3600 = 1 giờ.",
         "Mute for (seconds)", "0 = unmute; -1 = use the Telegram default; positive = mute for that many seconds (max 1 year: 31622400). Example: 3600 = 1 hour.", -1, 31622400)
USER = F("user_id", "int", False, None, "ID người dùng", "Số ID Telegram của người. Có thể thay bằng ô «Chọn người từ danh bạ» hoặc «Người nhận».",
         "User ID", "Numeric Telegram user ID. May be replaced by the contact picker or the recipient field.", 1)
MEMBER = F("user_id", "int", True, None, "ID người dùng", "Số ID Telegram của người cần thêm/mời ra. Tìm bằng dịch vụ find_user hoặc get_contacts.",
           "User ID", "Numeric Telegram user ID of the member. Find it with find_user or get_contacts.", 1)
MSG_ID = F("message_id", "int", True, None, "ID tin nhắn", "Số ID của tin nhắn (lấy từ get_history hoặc kết quả gửi tin).", "Message ID", "Numeric message ID (from get_history or a send result).", 1)

SERVICES: dict[str, S] = {
    # -- hội thoại ---------------------------------------------------------------------------------------
    "set_mute": S("chat", "optional", "Tắt/bật thông báo", "Tắt thông báo một cuộc trò chuyện trong một khoảng thời gian, hoặc bật lại.", "Mute chat", "Mute a chat for a while, or unmute it.", (MUTE,)),
    "pin_chat": S("chat", "optional", "Ghim/bỏ ghim hội thoại", "Ghim cuộc trò chuyện lên đầu danh sách (tối đa 5 cuộc ở danh sách chính).", "Pin chat", "Pin a chat to the top of the chat list (up to 5 in the main list).",
                  (F("pinned", "bool", False, True, "Ghim", "Bật = ghim, tắt = bỏ ghim.", "Pinned", "On = pin, off = unpin."),
                   F("archived", "bool", False, False, "Trong mục lưu trữ", "Bật nếu cuộc trò chuyện đang nằm trong mục lưu trữ.", "In archive", "On if the chat is in the archive."))),
    "archive_chat": S("chat", "optional", "Lưu trữ hội thoại", "Chuyển cuộc trò chuyện vào mục lưu trữ hoặc đưa ra lại danh sách chính.", "Archive chat", "Move a chat to the archive, or back to the main list.",
                      (F("archive", "bool", False, True, "Lưu trữ", "Bật = lưu trữ, tắt = đưa ra danh sách chính.", "Archive", "On = archive, off = move back to the main list."),)),
    "mark_chat_unread": S("chat", "optional", "Đánh dấu chưa đọc", "Gắn hoặc bỏ dấu «chưa đọc» của cuộc trò chuyện.", "Mark chat unread", "Set or clear the «unread» mark of a chat.",
                          (F("unread", "bool", False, True, "Chưa đọc", "Bật = đánh dấu chưa đọc, tắt = bỏ dấu.", "Unread", "On = mark unread, off = clear the mark."),)),
    "set_auto_delete": S("chat", "optional", "Tự xoá tin nhắn", "Đặt thời gian tự xoá tin nhắn mới của cuộc trò chuyện (0 = tắt).", "Auto-delete messages", "Set the auto-delete timer for new messages in a chat (0 = off).",
                         (F("seconds", "int", False, 0, "Sau (giây)", "0 = tắt; thường dùng 86400 (1 ngày), 604800 (1 tuần), 2678400 (1 tháng).", "After (seconds)", "0 = off; usually 86400 (1 day), 604800 (1 week), 2678400 (1 month).", 0, 31622400),)),
    "send_typing": S("chat", "optional", "Gửi trạng thái «đang gõ»", "Hiện «đang gõ…» hoặc «đang ghi âm…» ở phía người kia vài giây.", "Send typing action", "Show «typing…» or «recording…» to the other side for a few seconds.",
                     (F("action", "select", False, "typing", "Trạng thái", "typing = đang gõ; recording_voice = đang ghi âm; cancel = tắt trạng thái.", "Action", "typing; recording_voice; or cancel.", options=("typing", "recording_voice", "cancel")),)),
    "delete_chat_history": S("chat", "optional", "Xoá lịch sử hội thoại", "XOÁ toàn bộ tin nhắn của cuộc trò chuyện — KHÔNG hoàn tác được.", "Delete chat history", "DELETE all messages of a chat — cannot be undone.",
                             (F("revoke", "bool", False, False, "Xoá cả phía người kia", "Bật = xoá luôn ở phía người kia (nếu Telegram cho phép).", "Also delete for the other side", "On = also delete for the other side (when Telegram allows it)."),
                              F("remove_from_list", "bool", False, False, "Bỏ khỏi danh sách", "Bật = bỏ luôn cuộc trò chuyện khỏi danh sách.", "Remove from list", "On = also remove the chat from the list."))),
    "rename_chat": S("chat", "optional", "Đổi tên nhóm", "Đổi tên nhóm hoặc kênh (cần quyền quản trị).", "Rename group", "Change the title of a group or channel (admin rights needed).",
                     (F("title", "text", True, None, "Tên mới", "1-128 ký tự.", "New title", "1-128 characters."),), fill=()),
    "leave_chat": S("chat", "optional", "Rời nhóm", "Rời khỏi nhóm hoặc kênh.", "Leave group", "Leave a group or channel.", fill=()),
    "get_chat_info": S("chat", "only", "Xem thông tin hội thoại", "Trả về tên, loại, số tin chưa đọc, thời gian tắt thông báo và hẹn giờ tự xoá.", "Get chat info", "Return the title, kind, unread count, mute time and auto-delete timer.", ()),
    # -- tin nhắn ----------------------------------------------------------------------------------------
    "pin_message": S("chat", "optional", "Ghim tin nhắn", "Ghim một tin nhắn trong cuộc trò chuyện.", "Pin message", "Pin a message in a chat.",
                     (MSG_ID, F("notify", "bool", False, False, "Báo cho mọi người", "Bật = gửi thông báo ghim cho các thành viên.", "Notify members", "On = notify members about the pin."),
                      F("only_for_self", "bool", False, False, "Chỉ ghim cho tôi", "Bật = chỉ ghim ở phía bạn (hội thoại riêng).", "Only for me", "On = pin only on your side (private chats).")), fill=()),
    "unpin_message": S("chat", "optional", "Bỏ ghim tin nhắn", "Bỏ ghim một tin nhắn.", "Unpin message", "Unpin a message.", (MSG_ID,), fill=()),
    "send_poll": S("chat", "optional", "Gửi bình chọn", "Gửi bình chọn (2-10 lựa chọn) hoặc câu đố có đáp án đúng.", "Send poll", "Send a poll (2-10 options) or a quiz with a correct answer.",
                   (F("question", "text", True, None, "Câu hỏi", "1-300 ký tự.", "Question", "1-300 characters."),
                    F("options", "list_text", True, None, "Các lựa chọn", "Mỗi dòng một lựa chọn, 2-10 lựa chọn.", "Options", "One option per line, 2-10 options."),
                    F("anonymous", "bool", False, True, "Ẩn danh", "Bật = người xem không biết ai bầu gì.", "Anonymous", "On = voters are hidden."),
                    F("multiple", "bool", False, False, "Cho chọn nhiều", "Bật = được chọn nhiều đáp án (không dùng cho câu đố).", "Allow multiple answers", "On = several answers allowed (not for quizzes)."),
                    F("quiz_answer", "int", False, None, "Câu đố: đáp án đúng", "Số thứ tự (từ 1) của đáp án đúng; điền thì thành câu đố.", "Quiz: correct answer", "1-based number of the correct option; set it to make a quiz.", 1, 10),
                    F("explanation", "text", False, "", "Câu đố: giải thích", "Hiện sau khi trả lời (chỉ câu đố).", "Quiz: explanation", "Shown after answering (quizzes only)."))),
    "vote_poll": S("chat", "optional", "Bỏ phiếu bình chọn", "Chọn đáp án cho một bình chọn (đánh số từ 1).", "Vote in poll", "Vote in a poll (options numbered from 1).",
                   (MSG_ID, F("options", "list_int", True, None, "Đáp án chọn", "Số thứ tự đáp án (từ 1), nhiều đáp án cách nhau bằng dấu phẩy. Để trống = rút phiếu.", "Chosen options", "1-based option numbers, comma separated. Empty = retract the vote.")), fill=()),
    "stop_poll": S("chat", "optional", "Đóng bình chọn", "Đóng một bình chọn do bạn gửi.", "Stop poll", "Close a poll you sent.", (MSG_ID,), fill=()),
    # -- người dùng / danh bạ ----------------------------------------------------------------------------
    "find_user": S("none", "only", "Tìm người dùng", "Tìm người theo số điện thoại (có mã nước) hoặc @username công khai. Nêu ĐÚNG MỘT trong hai.", "Find user", "Find a person by phone number (with country code) or public @username. Give exactly ONE of them.",
                   (F("phone", "text", False, "", "Số điện thoại", "Ví dụ +84900000001.", "Phone number", "For example +84900000001."),
                    F("username", "text", False, "", "@username", "Tên người dùng công khai, có hoặc không có @.", "@username", "Public username, with or without @."))),
    "get_user_info": S("user", "only", "Xem thông tin người dùng", "Trả về tên, @username, số điện thoại (nếu được xem) và trạng thái trực tuyến.", "Get user info", "Return name, @username, phone (when visible) and online status."),
    "add_contact": S("user", "optional", "Thêm vào danh bạ", "Lưu một người vào danh bạ Telegram của tài khoản.", "Add contact", "Save a person to the account's Telegram contacts.",
                     (F("first_name", "text", True, None, "Tên", "Tên hiển thị trong danh bạ.", "First name", "Name shown in contacts."),
                      F("last_name", "text", False, "", "Họ", "Không bắt buộc.", "Last name", "Optional."),
                      F("phone", "text", False, "", "Số điện thoại", "Không bắt buộc; lưu cùng danh bạ.", "Phone number", "Optional; stored with the contact."),
                      F("share_phone", "bool", False, False, "Chia sẻ số của tôi", "Bật = để người đó thấy số của bạn.", "Share my number", "On = let them see your number."))),
    "remove_contact": S("user", "optional", "Xoá khỏi danh bạ", "Xoá một người khỏi danh bạ Telegram (không xoá cuộc trò chuyện).", "Remove contact", "Remove a person from Telegram contacts (the chat stays)."),
    "block_user": S("user", "optional", "Chặn người dùng", "Chặn một người: họ không nhắn hay gọi được cho bạn.", "Block user", "Block a person: they can no longer message or call you."),
    "unblock_user": S("user", "optional", "Bỏ chặn người dùng", "Bỏ chặn một người.", "Unblock user", "Unblock a person."),
    # -- nhóm --------------------------------------------------------------------------------------------
    "create_group": S("none", "optional", "Tạo nhóm", "Tạo nhóm mới với các thành viên (nhóm cơ bản; lên siêu nhóm khi cần).", "Create group", "Create a new group with members (basic group).",
                      (F("title", "text", True, None, "Tên nhóm", "1-128 ký tự.", "Group title", "1-128 characters."),
                       F("user_ids", "list_int", True, None, "Thành viên", "ID Telegram của các thành viên, cách nhau bằng dấu phẩy.", "Members", "Telegram user IDs, comma separated."))),
    "add_chat_member": S("chat", "optional", "Thêm thành viên vào nhóm", "Mời một người vào nhóm.", "Add group member", "Add a person to a group.",
                         (MEMBER, F("forward_limit", "int", False, 0, "Số tin cũ cho xem", "Số tin gần nhất người mới được thấy (0-100); chỉ cho nhóm cơ bản.", "Old messages shown", "Number of recent messages the new member sees (0-100); basic groups only.", 0, 100)), fill=()),
    "remove_chat_member": S("chat", "optional", "Mời thành viên ra khỏi nhóm", "Mời một người ra khỏi nhóm (cấm khoảng 1 phút, sau đó mời lại được; không cấm vĩnh viễn).", "Remove group member", "Remove a person from a group (banned for about a minute, then can be re-added).", (MEMBER,), fill=()),
    "create_invite_link": S("chat", "optional", "Tạo link mời nhóm", "Tạo link mời tham gia nhóm hoặc kênh.", "Create invite link", "Create an invite link for a group or channel.",
                            (F("name", "text", False, "", "Tên link", "Tên gợi nhớ, chỉ quản trị thấy.", "Link name", "A label only admins see."),
                             F("expire_seconds", "int", False, 0, "Hết hạn sau (giây)", "0 = không hết hạn.", "Expires after (seconds)", "0 = never expires.", 0, 31622400),
                             F("member_limit", "int", False, 0, "Giới hạn người vào", "0 = không giới hạn.", "Member limit", "0 = unlimited.", 0, 99999),
                             F("join_request", "bool", False, False, "Cần duyệt", "Bật = người vào phải được quản trị duyệt.", "Needs approval", "On = joiners must be approved by an admin.")), fill=()),
    "join_chat": S("none", "optional", "Tham gia nhóm bằng link", "Vào nhóm hoặc kênh bằng link mời t.me.", "Join by invite link", "Join a group or channel with a t.me invite link.",
                   (F("invite_link", "text", True, None, "Link mời", "Dạng https://t.me/+abc… hoặc https://t.me/joinchat/….", "Invite link", "Like https://t.me/+abc… or https://t.me/joinchat/…."),)),
    # -- tài khoản ---------------------------------------------------------------------------------------
    "get_me": S("none", "only", "Xem tài khoản đang dùng", "Trả về tên, @username và số điện thoại của tài khoản Telegram đang đăng nhập.", "Get my account", "Return name, @username and phone of the logged-in Telegram account."),
    "set_name": S("none", "optional", "Đổi tên tài khoản", "Đổi tên hiển thị của tài khoản.", "Change account name", "Change the account's display name.",
                  (F("first_name", "text", True, None, "Tên", "1-64 ký tự.", "First name", "1-64 characters."), F("last_name", "text", False, "", "Họ", "Tối đa 64 ký tự.", "Last name", "Up to 64 characters."))),
    "set_bio": S("none", "optional", "Đổi giới thiệu", "Đổi dòng giới thiệu (bio) của tài khoản.", "Change bio", "Change the account's bio.", (F("bio", "text", False, "", "Giới thiệu", "Tối đa 70 ký tự; để trống = xoá.", "Bio", "Up to 70 characters; empty = clear."),)),
    "set_username": S("none", "optional", "Đổi @username", "Đổi tên người dùng công khai (@username) của tài khoản.", "Change @username", "Change the account's public @username.", (F("username", "text", False, "", "@username", "5-32 ký tự a-z, 0-9, _; để trống = bỏ.", "@username", "5-32 characters a-z, 0-9, _; empty = remove."),)),
}
