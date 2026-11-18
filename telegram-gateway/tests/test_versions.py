"""Thương lượng phiên bản tgcalls: khớp NTgCalls (cao nhất chung), mô tả V2/V3, và gateway ngắt rõ ràng khi không có phiên bản chung. Không mạng."""
from gateway import live_call, versions

OURS = ["8.0.0", "9.0.0", "12.0.0", "13.0.0"]


def test_best_match_picks_highest_common_and_sorts_numerically():
    assert versions.best_match(["8.0.0"], OURS) == "8.0.0"                       # điện thoại như đã đo: chỉ 8.0.0
    assert versions.best_match(["2.4.4", "8.0.0", "13.0.0", "9.0.0"], OURS) == "13.0.0"
    assert versions.best_match(["12.0.0", "9.0.0"], OURS) == "12.0.0"
    assert versions.best_match(["10.0.0", "9.0.0"], OURS) == "9.0.0"             # 10.0.0 ta không hỗ trợ, rơi về 9.0.0 (so số, không so chuỗi: 10 > 9)
    assert versions.best_match(["2.4.4", "5.0.0"], OURS) is None
    assert versions.best_match([], OURS) is None and versions.best_match(None, OURS) is None
    assert versions.best_match(["x.y", "8.0.0"], OURS) == "8.0.0"                # chuỗi lạ không làm hỏng


def test_describe_names_the_signaling_family_and_old_clients():
    assert versions.describe(["8.0.0"], OURS) == ("8.0.0", "8.0.0 (báo hiệu V2 Legacy)")
    assert versions.describe(["13.0.0", "8.0.0"], OURS)[1] == "13.0.0 (báo hiệu V3 SCTP)"
    v, txt = versions.describe(["2.4.4", "5.0.0"], OURS)
    assert v is None and "KHÔNG có phiên bản chung" in txt and "cần cập nhật Telegram" in txt
    v, txt = versions.describe(["99.0.0"], OURS)
    assert v is None and "KHÔNG có phiên bản chung" in txt and "cập nhật" not in txt
    assert versions.describe([], OURS) == (None, "bên kia không khai phiên bản")


class StubRuntime:
    data_dir = "/tmp/live-test"

    def __init__(self, ours=None):
        self.ran, self._ours = [], ours if ours is not None else OURS

    def run(self, coro):
        self.ran.append(coro.cr_code.co_name)          # tên coroutine: «_ready» = dựng media, «_stop» = dọn khi kết thúc
        coro.close()

    def protocol(self):
        return {"library_versions": list(self._ours)}


class StubClient:
    def __init__(self):
        self.discarded = []

    def discard_call(self, cid, duration=0, is_video=False):
        self.discarded.append(cid)


def _ready(versions_list):
    return {"state": {"@type": "callStateReady", "emojis": ["a"], "servers": [], "allow_p2p": True, "protocol": {"library_versions": versions_list}}}


def test_ready_with_common_version_connects_and_logs_the_negotiated_family():
    rt, cl = StubRuntime(), StubClient()
    c = live_call.LiveCall(rt, "+84", cl, 5, True, False)
    c.tdlib_call_id = 7
    c.on_call_state(_ready(["8.0.0"]))
    assert c.state == "connecting" and rt.ran == ["_ready"] and cl.discarded == []
    assert any("thương lượng=8.0.0 (báo hiệu V2 Legacy)" in e["e"] for e in c.events)
    d = live_call.LiveCall(rt, "+84", cl, 5, True, False)
    d.tdlib_call_id = 8
    d.on_call_state(_ready(["13.0.0", "8.0.0"]))
    assert any("thương lượng=13.0.0 (báo hiệu V3 SCTP)" in e["e"] for e in d.events)


def test_ready_with_no_common_version_hangs_up_with_a_clear_reason_instead_of_hanging():
    rt, cl = StubRuntime(), StubClient()
    c = live_call.LiveCall(rt, "+84", cl, 5, False, False)
    c.tdlib_call_id = 9
    c.on_call_state(_ready(["2.4.4", "5.0.0"]))
    assert c.state == "ended" and cl.discarded == [9] and rt.ran == ["_stop"]   # không dựng media (không có _ready), đã discard
    assert "không tương thích" in c.error and "cần cập nhật Telegram" in c.error, c.error


def test_ready_without_peer_version_info_keeps_the_old_behaviour():
    rt, cl = StubRuntime(), StubClient()
    c = live_call.LiveCall(rt, "+84", cl, 5, True, False)
    c.tdlib_call_id = 7
    c.on_call_state({"state": {"@type": "callStateReady", "emojis": [], "servers": [], "allow_p2p": True}})
    assert c.state == "connecting" and rt.ran == ["_ready"] and cl.discarded == []
