"""Giao diện trò chuyện: luồng nhận tin chung, dịch dữ liệu, dịch vụ trò chuyện và đường REST. Không mạng."""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

import schema
from gateway import chat
from gateway.config import Config
from gateway.routes import build_app
from gateway.tdclient import TdlibClient, TdPump


# --- luồng nhận tin chung: phản hồi và update không bị client khác cướp -----------------------------

class MultiClientTransport:
    """Giống td_receive thật: MỘT hàng đợi chung cho mọi client, mỗi tin mang @client_id."""

    def __init__(self):
        self._q = []
        self._lock = threading.Lock()
        self._next = 1
        self.sent = []

    def create_client_id(self):
        with self._lock:
            cid, self._next = self._next, self._next + 1
        return cid

    def execute(self, data):
        return None

    def push(self, obj):
        with self._lock:
            self._q.append(json.dumps(obj).encode())

    def send(self, client_id, data):
        req = json.loads(data.decode())
        schema.validate(req)
        self.sent.append((client_id, req["@type"]))
        self.push({"@type": "ok", "@extra": req["@extra"], "@client_id": client_id,
                   "who": client_id})

    def receive(self, timeout):
        with self._lock:
            return self._q.pop(0) if self._q else None


def test_pump_routes_responses_and_updates_to_the_right_client(tmp_path):
    tr = MultiClientTransport()
    got = {1: [], 2: []}
    a = TdlibClient(str(tmp_path / "a"), 1, "0" * 32, transport=tr, on_update=lambda o: got[1].append(o))
    b = TdlibClient(str(tmp_path / "b"), 1, "0" * 32, transport=tr, on_update=lambda o: got[2].append(o))
    a.open(); b.open()
    assert (a.client_id, b.client_id) == (1, 2)
    tr.push({"@type": "updateNewMessage", "@client_id": 2, "message": {"id": 7}})
    tr.push({"@type": "updateNewMessage", "@client_id": 1, "message": {"id": 5}})
    assert a.call({"@type": "getMe"})["who"] == 1 and b.call({"@type": "getMe"})["who"] == 2
    for _ in range(100):
        if got[1] and got[2]:
            break
        time.sleep(0.02)
    assert [o["message"]["id"] for o in got[1]] == [5] and [o["message"]["id"] for o in got[2]] == [7]
    a.close(); b.close()


def test_pump_delivers_updates_without_any_pending_call(tmp_path):
    """Kiểu cũ chỉ nhận update khi có lệnh đang chờ: tin mới phải tới ngay cả khi không ai gọi gì."""
    tr = MultiClientTransport()
    seen = []
    c = TdlibClient(str(tmp_path), 1, "0" * 32, transport=tr, on_update=seen.append)
    c.open()
    tr.push({"@type": "updateNewMessage", "@client_id": c.client_id, "message": {"id": 1}})
    for _ in range(100):
        if seen:
            break
        time.sleep(0.02)
    assert seen and seen[0]["message"]["id"] == 1
    c.close()


def test_pump_request_times_out_cleanly(tmp_path):
    class Silent(MultiClientTransport):
        def send(self, client_id, data):
            pass
    c = TdlibClient(str(tmp_path), 1, "0" * 32, transport=Silent())
    c.client_id = Silent().create_client_id()
    c._pump.register(c.client_id, c)
    with pytest.raises(TimeoutError):
        c.call({"@type": "getMe"}, timeout=0.2)
    c._pump.unregister(c.client_id)


# --- dịch dữ liệu --------------------------------------------------------------------------------

def test_normalize_text_photo_voice_and_document():
    m = chat.normalize_message({"id": 9, "chat_id": 3, "date": 100, "is_outgoing": True,
                                "content": {"@type": "messageText", "text": {"text": "xin chào"}},
                                "interaction_info": {"reactions": {"reactions": [
                                    {"type": {"emoji": "👍"}, "total_count": 2, "is_chosen": True}]}}}, "An")
    assert m["kind"] == "text" and m["text"] == "xin chào" and m["outgoing"] and m["sender"] == "An"
    assert m["reactions"] == [{"emoji": "👍", "count": 2, "mine": True}]
    p = chat.normalize_message({"id": 1, "content": {"@type": "messagePhoto", "caption": {"text": "cap"},
                                "photo": {"sizes": [{"width": 90, "photo": {"id": 11}},
                                                    {"width": 800, "photo": {"id": 12, "local": {
                                                        "is_downloading_completed": True, "path": "/x.jpg"}}},
                                                    {"width": 2000, "photo": {"id": 13}}]}}})
    assert p["kind"] == "photo" and p["file"]["id"] == 12 and p["file"]["path"] == "/x.jpg"   # bỏ cỡ > 1280
    assert p["text"] == "cap" and "Ảnh" in p["summary"]
    v = chat.normalize_message({"id": 2, "content": {"@type": "messageVoiceNote",
                                "voice_note": {"duration": 4, "voice": {"id": 21}}}})
    assert v["kind"] == "voice" and v["file"]["id"] == 21 and v["file"]["duration"] == 4
    d = chat.normalize_message({"id": 3, "content": {"@type": "messageDocument", "document": {
        "file_name": "a.pdf", "mime_type": "application/pdf", "document": {"id": 31, "size": 10}}}})
    assert d["file"]["name"] == "a.pdf" and d["file"]["size"] == 10 and "a.pdf" in d["summary"]
    assert chat.normalize_message({"id": 4, "content": {"@type": "messageWeird"}})["kind"] == "other"


def test_chat_kind_mapping():
    assert chat.chat_kind({"@type": "chatTypePrivate"}) == "private"
    assert chat.chat_kind({"@type": "chatTypeSupergroup", "is_channel": True}) == "channel"
    assert chat.chat_kind({"@type": "chatTypeSupergroup", "is_channel": False}) == "group"
    assert chat.chat_kind({"@type": "chatTypeBasicGroup"}) == "group"


def test_feed_cursor_longpoll_and_reset():
    f = chat.Feed(maxlen=3)
    assert f.since(0)["items"] == []
    f.push("chat", chat_id=1)
    r = f.since(0)
    assert [i["type"] for i in r["items"]] == ["chat"] and r["cursor"] == 1
    t0 = time.time()
    assert f.since(1, wait=0.2)["items"] == [] and time.time() - t0 >= 0.18           # chờ rồi trả rỗng
    threading.Timer(0.1, lambda: f.push("message", chat_id=2)).start()
    assert f.since(1, wait=2)["items"][0]["chat_id"] == 2                              # long-poll bị đánh thức
    for i in range(5):
        f.push("chat", chat_id=i)
    assert f.since(2)["reset"] is True                                                  # cursor cũ hơn phần còn giữ


# --- ChatService với client giả ------------------------------------------------------------------

class FakeTd:
    def __init__(self):
        self.calls = []
        self.on_update = None
        self.users = {10: {"id": 10, "first_name": "Bình", "last_name": "Lê"}}

    def call(self, req, timeout=30):
        schema.validate(req)                 # mọi yêu cầu gửi cho TDLib phải đúng lược đồ thật
        self.calls.append(req)
        t = req["@type"]
        if t == "getMe":
            return {"id": 1, "first_name": "Tôi", "phone_number": "84900"}
        if t == "getUser":
            return self.users[req["user_id"]]
        if t == "loadChats":
            raise Exception("404")
        if t == "getChats":
            return {"chat_ids": [100, 200]}
        if t == "getChat":
            return {"id": req["chat_id"], "title": "Nhóm" if req["chat_id"] == 200 else "Bình",
                    "type": {"@type": "chatTypePrivate", "user_id": 10} if req["chat_id"] == 100
                    else {"@type": "chatTypeSupergroup", "is_channel": False},
                    "unread_count": 3, "positions": [{"is_pinned": req["chat_id"] == 100}],
                    "last_message": {"date": 50, "is_outgoing": False,
                                     "content": {"@type": "messageText", "text": {"text": "x" * 300}}}}
        if t == "getChatHistory":
            if req["from_message_id"] == 0:
                return {"messages": [self._msg(5), self._msg(4)]}
            return {"messages": [self._msg(3)]}
        if t == "sendMessage":
            return {"id": 77, "chat_id": req["chat_id"], "date": 1, "is_outgoing": True,
                    "content": {"@type": "messageText", "text": {"text": (req["input_message_content"].get("text") or {}).get("text", "")}}}
        return {"@type": "ok"}

    @staticmethod
    def _msg(i):
        return {"id": i, "chat_id": 100, "date": i, "sender_id": {"@type": "messageSenderUser", "user_id": 10},
                "content": {"@type": "messageText", "text": {"text": f"tin {i}"}}}


def test_chat_service_lists_chats_history_and_sends():
    td = FakeTd()
    svc = chat.ChatService(td)
    cs = svc.chats()
    assert [c["id"] for c in cs] == [100, 200] and cs[0]["pinned"] and cs[0]["kind"] == "private"
    assert cs[1]["kind"] == "group" and len(cs[0]["last"]["text"]) <= chat.PREVIEW_MAX
    h = svc.history(100, limit=3)
    assert [m["id"] for m in h] == [3, 4, 5] and h[0]["sender"] == "Bình Lê"          # tăng dần, có tên người gửi
    assert sum(1 for c in td.calls if c["@type"] == "getUser") == 1                    # tên người gửi được nhớ
    out = svc.send_text(100, "chào", reply_to=4)
    sent = [c for c in td.calls if c["@type"] == "sendMessage"][0]
    assert sent["reply_to"]["message_id"] == 4 and out["outgoing"] and out["text"] == "chào"
    svc.send_file(100, "/data/uploads/a.jpg", "photo", "đây")
    last = [c for c in td.calls if c["@type"] == "sendMessage"][-1]
    assert last["input_message_content"]["@type"] == "inputMessagePhoto"
    svc.send_file(100, "/x.bin", "weird")
    assert [c for c in td.calls if c["@type"] == "sendMessage"][-1]["input_message_content"]["@type"] == "inputMessageDocument"


def test_chat_service_updates_feed_without_network_calls():
    td = FakeTd()
    svc = chat.ChatService(td)
    svc._users[10] = td.users[10]
    n = len(td.calls)
    svc.handle_update({"@type": "updateNewMessage", "message": FakeTd._msg(9)})
    svc.handle_update({"@type": "updateChatLastMessage", "chat_id": 100})
    svc.handle_update({"@type": "updateDeleteMessages", "chat_id": 100, "message_ids": [1], "is_permanent": True})
    svc.handle_update({"@type": "updateDeleteMessages", "chat_id": 100, "message_ids": [2], "is_permanent": False})
    items = svc.feed.since(0)["items"]
    assert [i["type"] for i in items] == ["message", "chat", "deleted"]
    assert items[0]["message"]["sender"] == "Bình Lê" and len(td.calls) == n           # không gọi mạng trong luồng nhận


# --- REST ----------------------------------------------------------------------------------------

def make_app(tmp_path, with_client=True):
    cfg = Config(data_dir=str(tmp_path), gateway_token="tk")
    app = build_app(cfg, clients={"+84900000000": FakeTd()} if with_client else {})
    return TestClient(app, raise_server_exceptions=False), app


H = {"Authorization": "Bearer tk"}


def test_chat_routes_require_token_and_account(tmp_path):
    client, _ = make_app(tmp_path, with_client=False)
    for path in ("/api/chats", "/api/contacts", "/api/me", "/api/feed", "/api/search?q=a"):
        assert client.get(path).status_code == 401
    r = client.get("/api/chats", headers=H)
    assert r.status_code == 503 and r.json()["error"]["code"] == "ACCOUNT_OFFLINE"


def test_chat_routes_happy_paths(tmp_path):
    client, app = make_app(tmp_path)
    assert client.get("/api/me", headers=H).json()["name"] == "Tôi"
    assert [c["id"] for c in client.get("/api/chats", headers=H).json()["chats"]] == [100, 200]
    msgs = client.get("/api/chats/100/messages?limit=3", headers=H).json()["messages"]
    assert [m["id"] for m in msgs] == [3, 4, 5]
    assert client.post("/api/chats/100/send", headers=H, json={"text": "   "}).status_code == 400
    assert client.post("/api/chats/100/send", headers=H, json={"text": "x" * 5000}).status_code == 400
    r = client.post("/api/chats/100/send", headers=H, json={"text": "ok"})
    assert r.status_code == 200 and r.json()["id"] == 77
    # Gửi tệp: thân là nội dung tệp thô; kind sai bị từ chối; tên bị rửa đường dẫn.
    assert client.post("/api/chats/100/send_file?kind=exe", headers=H, content=b"x").status_code == 400
    assert client.post("/api/chats/100/send_file?kind=photo", headers=H, content=b"").status_code == 400
    r = client.post("/api/chats/100/send_file?kind=photo&name=../../etc/a.jpg", headers=H, content=b"jpegdata")
    assert r.status_code == 200
    td = app.state.clients["+84900000000"]
    path = [c for c in td.calls if c["@type"] == "sendMessage"][-1]["input_message_content"]["photo"]["photo"]["path"]
    assert path.startswith(str(tmp_path / "uploads")) and ".." not in path.replace(str(tmp_path), "")
    assert client.post("/api/chats/100/read", headers=H, json={"message_ids": [1, 2]}).json() == {"ok": True}
    # Bảng tin: tin đến qua on_update xuất hiện ở /api/feed.
    td.on_update({"@type": "updateChatLastMessage", "chat_id": 100})
    f = client.get("/api/feed?since=0", headers=H).json()
    assert any(i["type"] == "chat" and i["chat_id"] == 100 for i in f["items"])
    assert client.get("/api/feed?since=abc", headers=H).status_code == 400
    assert client.get("/api/search?q=", headers=H).json() == {"chats": []}


def test_schema_validator_catches_the_old_shapes_that_broke_in_production():
    """Ba kiểu sai đã gặp thật: tệp không bọc, setTdlibParameters lồng, trường không tồn tại."""
    doc_old = {"@type": "inputMessageDocument", "caption": {"@type": "formattedText", "text": "", "entities": []},
               "document": {"@type": "inputFileLocal", "path": "/x"}}
    assert any("inputDocument" in e for e in schema.errors(doc_old))
    nested = {"@type": "setTdlibParameters", "parameters": {"@type": "tdlibParameters", "api_id": 1}}
    assert any("parameters" in e for e in schema.errors(nested))
    assert schema.errors({"@type": "getMe"}) == []
    assert any("không có trường" in e for e in schema.errors({"@type": "getMe", "bogus": 1}))
    # Gửi mọi loại tệp qua ChatService: FakeTd.call đã kiểm lược đồ từng yêu cầu.
    svc = chat.ChatService(FakeTd())
    for kind in ("photo", "video", "voice", "audio", "document"):
        svc.send_file(100, "/data/uploads/f", kind, "cap")


def test_send_result_updates_reach_the_feed():
    svc = chat.ChatService(FakeTd())
    msg = {"id": 900, "chat_id": 100, "date": 5, "is_outgoing": True, "content": {"@type": "messageText", "text": {"text": "ok"}}}
    svc.handle_update({"@type": "updateMessageSendSucceeded", "old_message_id": 77, "message": msg})
    svc.handle_update({"@type": "updateMessageSendFailed", "old_message_id": 78, "message": dict(msg, id=78),
                       "error": {"code": 400, "message": "FILE_PART_INVALID"}})
    ok, bad = svc.feed.since(0)["items"]
    assert ok["type"] == "send_ok" and ok["old_id"] == 77 and ok["message"]["id"] == 900
    assert bad["type"] == "send_failed" and bad["old_id"] == 78 and "FILE_PART" in bad["error"]


def test_provisional_message_id_is_swapped_for_the_real_one_before_edit_react_delete_forward():
    """sendMessage trả id TẠM; sửa/xoá/phản ứng/chuyển tiếp bằng id tạm thì Telegram báo «Message not found» (gặp thật)."""
    td = FakeTd()
    svc = chat.ChatService(td)
    tmp, real = 1789919233, 1789919232 + 1048576            # tạm: không phải bội của 2^20; thật: bội của 2^20
    svc.handle_update({"@type": "updateMessageSendSucceeded", "old_message_id": tmp,
                       "message": {"id": real, "chat_id": 100, "date": 5, "is_outgoing": True,
                                   "content": {"@type": "messageText", "text": {"text": "ok"}}}})
    svc.react(100, tmp, "👍")
    svc.forward(100, 100, [tmp])
    svc.delete(100, [tmp])
    ids = [(c["@type"], c.get("message_id") or c.get("message_ids")) for c in td.calls
           if c["@type"] in ("addMessageReaction", "forwardMessages", "deleteMessages")]
    assert ids == [("addMessageReaction", real), ("forwardMessages", [real]), ("deleteMessages", [real])], ids
    svc.react(100, 2 * 1048576, "👍")                        # id thật đưa vào thì giữ nguyên
    assert td.calls[-1]["message_id"] == 2 * 1048576


def test_provisional_id_waits_for_send_succeeded_then_gives_up_without_guessing():
    svc = chat.ChatService(FakeTd())
    tmp, real = 5, 3 * 1048576
    threading.Timer(0.2, lambda: svc._remember_sent(tmp, real)).start()
    t0 = time.time()
    assert svc.final_id(tmp, wait=3) == real and time.time() - t0 < 2          # đợi update tới rồi dùng
    assert svc.final_id(6, wait=0.2) == 6                                        # không có thì trả nguyên id, không đoán


def test_sent_id_map_is_bounded():
    svc = chat.ChatService(FakeTd())
    for i in range(1, 700):
        svc._remember_sent(i, i * 1048576)
    assert len(svc._sent_ids) == 500 and 1 not in svc._sent_ids and 699 in svc._sent_ids
