"""Rò tài nguyên trong gateway: mỗi bảng nhớ / tiến trình / phiên tải lên phải có đường dọn. Mỗi test PHÁ ĐƯỢC khi bỏ bản sửa (đã thử)."""
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from gateway import chat, live_call, tts
from gateway.calls import MAX_ENDED, CallManager


def test_abandoned_tts_upload_is_dropped_after_ttl(tmp_path):
    store = tts.TtsUploadStore(str(tmp_path))
    first = store.add_chunk(None, "QUJD")["upload_id"]                  # client bắt đầu rồi chết, không bao giờ gửi done
    assert first in store._parts
    store._created[first] -= tts.UPLOAD_TTL + 1
    second = store.add_chunk(None, "QUJD")["upload_id"]                 # lần tải kế tiếp quét phiên cũ
    assert first not in store._parts and first not in store._created and second in store._parts
    out = store.add_chunk(second, "", done=True)
    assert out["size"] == 3, "phiên còn sống vẫn hoàn tất bình thường"


class _Td:
    def __init__(self):
        self.on_update = None

    def call(self, req, timeout=30):
        return {"id": req.get("user_id", 1), "first_name": "U%s" % req.get("user_id", 1)}


def test_user_cache_is_capped_and_keeps_the_recently_used(monkeypatch):
    svc = chat.ChatService(_Td())
    for i in range(chat.USERS_MAX + 500):
        svc.handle_update({"@type": "updateUser", "user": {"id": i + 10, "first_name": "N%d" % i}})
    assert len(svc._users) == chat.USERS_MAX
    assert (chat.USERS_MAX + 500 + 9) in svc._users and 10 not in svc._users, "người cũ nhất đi trước, người mới nhất còn"
    keep = next(iter(svc._users))
    svc.user(keep)                                                         # dùng lại thì được giữ lâu hơn
    svc.handle_update({"@type": "updateUser", "user": {"id": 99999, "first_name": "Mới"}})
    assert keep in svc._users


def test_ended_legacy_calls_are_pruned_but_active_ones_never(monkeypatch):
    cm = CallManager()
    live = cm.start_call("a", "p", "tts")                                   # cuộc đang chạy: phải còn mãi
    for i in range(MAX_ENDED + 40):
        c = cm.start_call("b%d" % i, "p", "tts")
        cm.hangup(c.call_id)
    cm.start_call("zz", "p", "tts")                                         # lượt gọi mới kích hoạt dọn
    kept = cm.list_calls()
    assert len([c for c in kept if c.state == "ended"]) <= MAX_ENDED
    assert cm.get(live.call_id) is not None


def test_video_view_reaps_ffmpeg_instead_of_leaving_a_zombie():
    v = live_call.VideoView()
    proc = subprocess.Popen(["sleep", "30"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    v._proc = proc
    v.close()
    assert proc.returncode is not None, "close() phải thu xác (wait) tiến trình con, không chỉ kill"
    assert v._proc is None
