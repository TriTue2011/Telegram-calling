"""Dịch vụ theo danh mục (td_services): mỗi yêu cầu gửi TDLib phải ĐÚNG lược đồ td_api.tl (tests/schema.py), đầu vào sai bị từ chối, route chỉ chạy tên trong danh sách cho phép."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import schema  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway import td_services as ts  # noqa: E402
from gateway.config import Config  # noqa: E402
from gateway.routes import build_app  # noqa: E402


class Td:
    """Client TDLib giả: kiểm lược đồ MỌI yêu cầu, ghi lại, trả dữ liệu đúng hình."""

    def __init__(self):
        self.calls = []
        self.on_update = None

    def call(self, req, timeout=30):
        schema.validate(req)
        self.calls.append(req)
        t = req["@type"]
        if t == "getChat":
            return {"id": req["chat_id"], "title": "Nhóm A", "type": {"@type": "chatTypeBasicGroup"}, "unread_count": 2, "message_auto_delete_time": 0,
                    "notification_settings": {"@type": "chatNotificationSettings", "use_default_mute_for": True, "mute_for": 0, "use_default_sound": True, "sound_id": 0}}
        if t in ("getUser", "getMe", "searchUserByPhoneNumber"):
            return {"id": 77, "first_name": "Bình", "last_name": "Lê", "phone_number": "84900000001", "usernames": {"active_usernames": ["binh"]},
                    "status": {"@type": "userStatusOnline"}, "type": {"@type": "userTypeRegular"}}
        if t == "searchPublicChat":
            return {"id": 77, "title": "Bình", "type": {"@type": "chatTypePrivate", "user_id": 77}} if req["username"] == "binh" else {"id": -100, "title": "Kênh", "type": {"@type": "chatTypeSupergroup"}}
        if t == "createNewBasicGroupChat":
            return {"chat_id": -555, "failed_to_add_members": {"failed_to_add_members": [{"user_id": 3}]}}
        if t == "createChatInviteLink":
            return {"invite_link": "https://t.me/+abc"}
        if t == "joinChatByInviteLink":
            return {"chat_id": -555}
        if t == "sendMessage":
            return {"id": 4096}
        if t == "addChatMember":
            return {"failed_to_add_members": []}
        return {"@type": "ok"}


CASES = {
    "set_mute": {"chat_id": 5, "mute_for": 3600}, "pin_chat": {"chat_id": 5}, "archive_chat": {"chat_id": 5}, "mark_chat_unread": {"chat_id": 5},
    "set_auto_delete": {"chat_id": 5, "seconds": 86400}, "send_typing": {"chat_id": 5}, "delete_chat_history": {"chat_id": 5, "revoke": True},
    "rename_chat": {"chat_id": -5, "title": "Tên mới"}, "leave_chat": {"chat_id": -5}, "get_chat_info": {"chat_id": 5},
    "pin_message": {"chat_id": 5, "message_id": 100}, "unpin_message": {"chat_id": 5, "message_id": 100},
    "send_poll": {"chat_id": 5, "question": "Ăn gì?", "options": ["Phở", "Bún"]}, "vote_poll": {"chat_id": 5, "message_id": 100, "options": [1]},
    "stop_poll": {"chat_id": 5, "message_id": 100}, "find_user": {"phone": "+84 900 000 001"}, "get_user_info": {"user_id": 77},
    "add_contact": {"user_id": 77, "first_name": "Bình"}, "remove_contact": {"user_id": 77}, "block_user": {"user_id": 77}, "unblock_user": {"user_id": 77},
    "create_group": {"title": "Nhà", "user_ids": "1, 2, 3"}, "add_chat_member": {"chat_id": -5, "user_id": 77}, "remove_chat_member": {"chat_id": -5, "user_id": 77},
    "create_invite_link": {"chat_id": -5, "expire_seconds": 3600}, "join_chat": {"invite_link": "https://t.me/+abc"},
    "get_me": {}, "set_name": {"first_name": "An"}, "set_bio": {"bio": "xin chào"}, "set_username": {"username": "an_nhi"},
}


def test_every_service_builds_schema_valid_tdlib_requests_and_returns_a_dict():
    assert set(CASES) == set(ts.HANDLERS), "danh sách test phải phủ MỌI dịch vụ cho phép"
    for name, args in CASES.items():
        td = Td()
        res = ts.run_service(td, name, args)
        assert isinstance(res, dict) and td.calls, name


def test_requests_carry_the_right_fields():
    td = Td()
    ts.run_service(td, "set_mute", {"chat_id": 5, "mute_for": 3600})
    ns = td.calls[-1]["notification_settings"]
    assert td.calls[-1]["@type"] == "setChatNotificationSettings" and ns["mute_for"] == 3600 and ns["use_default_mute_for"] is False and ns["use_default_sound"] is True, "giữ các cài đặt khác"
    ts.run_service(td, "set_mute", {"chat_id": 5, "mute_for": -1})
    assert td.calls[-1]["notification_settings"]["use_default_mute_for"] is True
    ts.run_service(td, "pin_chat", {"chat_id": 5, "archived": True})
    assert td.calls[-1]["chat_list"] == {"@type": "chatListArchive"} and td.calls[-1]["is_pinned"] is True
    ts.run_service(td, "send_poll", {"chat_id": 5, "question": "Q", "options": "a\nb\nc", "multiple": True, "anonymous": False})
    c = td.calls[-1]["input_message_content"]
    assert [o["text"]["text"] for o in c["options"]] == ["a", "b", "c"] and c["allows_multiple_answers"] is True and c["is_anonymous"] is False
    ts.run_service(td, "send_poll", {"chat_id": 5, "question": "Q", "options": ["a", "b"], "quiz_answer": 2, "explanation": "vì sao"})
    c = td.calls[-1]["input_message_content"]
    assert c["type"]["@type"] == "inputPollTypeQuiz" and c["type"]["correct_option_ids"] == [1] and c["allows_multiple_answers"] is False
    ts.run_service(td, "vote_poll", {"chat_id": 5, "message_id": 9, "options": "1,3"})
    assert td.calls[-1]["option_ids"] == [0, 2], "người dùng đánh số từ 1, TDLib từ 0"
    r = ts.run_service(td, "find_user", {"username": "@binh"})
    assert r["user_id"] == 77 and r["username"] == "binh" and r["phone"] == "+84900000001"
    assert ts.run_service(td, "find_user", {"username": "kenh"})["user_id"] is None
    ts.run_service(td, "unblock_user", {"user_id": 77})
    assert "block_list" not in td.calls[-1], "bỏ chặn = không truyền block_list"
    r = ts.run_service(td, "create_group", {"title": "Nhà", "user_ids": [1, 2, 3]})
    assert r["chat_id"] == -555 and r["failed_user_ids"] == [3]
    ts.run_service(td, "remove_chat_member", {"chat_id": -5, "user_id": 77})
    assert td.calls[-1]["status"]["@type"] == "chatMemberStatusBanned" and td.calls[-1]["status"]["banned_until_date"] > 0
    assert ts.run_service(td, "create_invite_link", {"chat_id": -5})["invite_link"] == "https://t.me/+abc"


def test_bad_input_is_refused_before_anything_is_sent():
    bad = [("set_mute", {"chat_id": 5, "mute_for": 10 ** 9}), ("send_typing", {"chat_id": 5, "action": "bay"}), ("rename_chat", {"chat_id": 1, "title": " "}),
           ("send_poll", {"chat_id": 5, "question": "Q", "options": ["chỉ một"]}), ("send_poll", {"chat_id": 5, "question": "Q", "options": ["a", "b"], "quiz_answer": 3}),
           ("vote_poll", {"chat_id": 5, "message_id": 9, "options": [0]}), ("find_user", {}), ("find_user", {"phone": "1", "username": "a"}), ("add_contact", {"user_id": 1}),
           ("create_group", {"title": "x", "user_ids": []}), ("join_chat", {"invite_link": "https://evil.example/x"}), ("set_bio", {"bio": "x" * 71}), ("pin_chat", {"chat_id": "abc"})]
    for name, args in bad:
        td = Td()
        with pytest.raises(ValueError):
            ts.run_service(td, name, args)
        assert not td.calls, (name, "không được gửi gì cho TDLib khi đầu vào sai")


def test_route_runs_only_allowed_services_and_maps_errors(tmp_path):
    td = Td()
    app = build_app(Config(data_dir=str(tmp_path), gateway_token="tk"), clients={"+84900000000": td})
    c = TestClient(app, raise_server_exceptions=False)
    H = {"Authorization": "Bearer tk"}
    assert c.post("/api/do/get_me", json={}).status_code == 401
    assert c.post("/api/do/khong_co", headers=H, json={}).status_code == 404
    assert c.post("/api/do/pin_chat", headers=H, json={"chat_id": "abc"}).status_code == 400
    r = c.post("/api/do/pin_chat", headers=H, json={"chat_id": 5})
    assert r.status_code == 200 and r.json() == {"chat_id": 5, "pinned": True}
    assert c.post("/api/do/get_me", headers=H, json={}).json()["name"] == "Bình Lê"
    assert [x["@type"] for x in td.calls] == ["toggleChatIsPinned", "getMe"]
