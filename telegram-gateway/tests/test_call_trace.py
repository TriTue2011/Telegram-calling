"""Nhat ky CHI TIET tung cuoc goi Telegram (call_trace.py): ra tep, giu 5 cuoc, che bi mat, tran dung luong, hieu bo dem moi giay, noi vao LiveCall va API."""
import os
import re
import time

import pytest
from fastapi.testclient import TestClient

from gateway import call_trace as ct
from gateway import live_call
from gateway.config import Config
from gateway.routes import build_app


class StubRuntime:
    data_dir = "/tmp/live-test"
    calls = {}

    def run(self, coro):
        coro.close()


class StubClient:
    def discard_call(self, *a, **k):
        pass

    def accept_call(self, *a, **k):
        pass


def _setup(tmp_path, keep=5, max_kb=64, stats=None):
    ct.reset_for_tests()
    ct.KEEP, ct.MAX_BYTES, ct.SAMPLE_SECONDS = keep, max_kb * 1024, 0.0           # 0: khong luong lay mau; test goi sample_once truc tiep
    ct.configure(str(tmp_path), stats=stats, version="test-1")
    return tmp_path / "calltrace"


def _txt(d, cid):
    ct.reset_for_tests()
    return (d / ("%s.log" % cid)).read_text(encoding="utf-8")


def test_lines_go_to_the_calls_file_with_header_and_relative_time(tmp_path):
    d = _setup(tmp_path)
    ct.line("aaaaaaaaaa", "su-kien", "TDLib: Ready (servers=4)")
    ct.line("aaaaaaaaaa", "su-kien", "NTgCalls kết nối: CONNECTED")
    txt = _txt(d, "aaaaaaaaaa")
    assert "mo-dau call=aaaaaaaaaa gateway=test-1" in txt and "su-kien TDLib: Ready (servers=4)" in txt and "CONNECTED" in txt
    assert re.search(r"^\d\d:\d\d:\d\d\.\d{3} \+\d+\.\d{3}s su-kien ", txt, re.M)


def test_only_the_last_KEEP_calls_survive_and_keep_zero_turns_it_off(tmp_path):
    d = _setup(tmp_path, keep=5)
    for i in range(8):
        ct.line("%010x" % (i + 1), "su-kien", "x")
        ct._traces["%010x" % (i + 1)].closed = True
        time.sleep(0.02)
    ct._prune()
    assert sorted(f[:-4] for f in os.listdir(d)) == ["%010x" % i for i in range(4, 9)]
    d2 = _setup(tmp_path / "tat", keep=0)
    ct.line("bbbbbbbbbb", "su-kien", "x"); ct.packet("bbbbbbbbbb", "tx", b"abc")
    assert not d2.exists() and not ct.enabled()


def test_secrets_are_masked_and_env_secrets_never_listed(tmp_path, monkeypatch):
    d = _setup(tmp_path)
    monkeypatch.setenv("TELEGRAM_API_HASH", "hash-bi-mat"); monkeypatch.setenv("TELEGRAM_DEBUG", "1"); monkeypatch.setenv("TELEGRAM_GATEWAY_TOKEN", "tok-bi-mat")
    ct.line("cccccccccc", "su-kien", "khoa=" + "A" * 60 + " ok")
    txt = _txt(d, "cccccccccc")
    assert "hash-bi-mat" not in txt and "tok-bi-mat" not in txt and "A" * 40 not in txt and "<ẩn>" in txt
    assert "TELEGRAM_DEBUG=1" in txt


def test_size_cap_keeps_the_final_summary(tmp_path):
    d = _setup(tmp_path, max_kb=4)
    for i in range(400):
        ct.line("dddddddddd", "su-kien", "dong du dai de lam day tep so=%d %s" % (i, "x" * 30))
    ct.finish("dddddddddd", "TONG KET CUOI")
    txt = _txt(d, "dddddddddd")
    assert txt.count("trần dung lượng") == 1 and "TONG KET CUOI" in txt and len(txt.encode()) < 4 * 1024 + 2048


def test_signaling_packets_are_logged_one_by_one(tmp_path):
    d = _setup(tmp_path)
    ct.packet("eeeeeeeeee", "tx", b"\x01\x02\x03" + b"\x00" * 40)
    ct.packet("eeeeeeeeee", "rx", b"\xff" * 5)
    txt = _txt(d, "eeeeeeeeee")
    assert "goi-tx #1 baohieu len=43 hex=010203000000000000000000" in txt and "goi-rx #1 baohieu len=5 hex=ffffffffff" in txt


def test_per_second_sample_logs_DIFFERENCES_network_and_resources(tmp_path):
    cnt = {"khung_tieng_vao": 0, "bao_hieu_tx_goi": 0, "ten": "x"}
    d = _setup(tmp_path, stats=lambda cid: cnt)
    ct.line("ffffffffff", "su-kien", "bat dau")
    t = ct._traces["ffffffffff"]
    cnt["khung_tieng_vao"], cnt["bao_hieu_tx_goi"] = 50, 3
    ct.sample_once(t, 0)
    cnt["khung_tieng_vao"] = 100
    ct.sample_once(t, 1)
    ct.sample_once(t, 2)                                       # khong doi -> khong co dong dem-1s moi
    txt = _txt(d, "ffffffffff")
    lines = [ln for ln in txt.splitlines() if " dem-1s " in ln]
    assert len(lines) == 2 and "khung_tieng_vao+50" in lines[0] and "bao_hieu_tx_goi+3" in lines[0] and "bao_hieu_tx_goi" not in lines[1], lines
    assert "dem-tong " in txt and "khung_tieng_vao=50" in txt and "tai-nguyen cpu=" in txt and "rss=" in txt and "load=" in txt
    net = ct._net_totals()
    assert set(net) >= {"rx_pkt", "tx_pkt", "rx_drop", "tx_drop", "udp_drop"} and all(isinstance(v, int) for v in net.values())


def test_read_only_accepts_hex_ids_and_list_is_newest_first(tmp_path):
    _setup(tmp_path)
    ct.line("1111111111", "su-kien", "x"); time.sleep(0.02); ct.line("2222222222", "su-kien", "y")
    assert ct.read("../../etc/passwd") is None and ct.read("11/../22") is None and ct.read("ZZZ") is None
    assert "su-kien x" in ct.read("1111111111")
    assert [r["id"] for r in ct.list_traces()] == ["2222222222", "1111111111"]


def test_live_call_log_signaling_and_end_reach_the_trace_untruncated(tmp_path):
    rt = StubRuntime(); rt.calls = {}
    d = _setup(tmp_path, stats=lambda cid: rt.calls[cid].sig if cid in rt.calls else None)
    c = live_call.LiveCall(rt, "+84", StubClient(), 5, False)
    rt.calls[c.id] = c
    for i in range(300):                                        # nhieu hon MAX_EVENTS (200): nhat ky chi tiet khong bi cat
        c.log("su kien so %d" % i)
    c.on_signaling(b"\x10" * 20)
    c.tdlib_call_id = 9
    c.on_call_state({"state": {"@type": "callStateDiscarded", "reason": {"@type": "callDiscardReasonMissed"}}})
    txt = _txt(d, c.id)
    assert "su kien so 0" in txt and "su kien so 299" in txt, "chi con %d dong su-kien" % txt.count("su-kien")
    assert "goi-rx #1 baohieu len=20" in txt and "Missed" in txt and "tong-ket " in txt
    assert '"state": "ended"' in txt.split("tong-ket ")[1], "tong ket phai co trang thai cuoi"


def test_api_lists_and_downloads_with_token_only(tmp_path):
    ct.reset_for_tests()
    cfg = Config(data_dir=str(tmp_path), gateway_token="test-token", rate_limit_per_minute=1000)
    client = TestClient(build_app(cfg), raise_server_exceptions=False)
    ct.KEEP, ct.MAX_BYTES, ct.SAMPLE_SECONDS = 5, 64 * 1024, 0.0
    ct.line("abcdef0123", "su-kien", "noi dung thu")
    h = {"Authorization": "Bearer test-token"}
    assert client.get("/api/calltrace").status_code == 401 and client.get("/api/calltrace/abcdef0123").status_code == 401
    r = client.get("/api/calltrace", headers=h).json()
    assert r["ok"] and r["enabled"] and "abcdef0123" in [x["id"] for x in r["calls"]]
    t = client.get("/api/calltrace/abcdef0123", headers=h)
    assert t.status_code == 200 and t.headers["content-type"].startswith("text/plain") and "noi dung thu" in t.text
    assert client.get("/api/calltrace/khong-co", headers=h).status_code == 404 and client.get("/api/calltrace/abcdef0124", headers=h).status_code == 404
    ct.reset_for_tests()


def _feed(monkeypatch, t, rx_rates, tx_rate=2000):
    """Giả lập giao diện mạng: mỗi giây nhận rx_rates[i] byte, gửi tx_rate byte; chạy sample_once từng giây."""
    state = {"rx": 0, "tx": 0}
    monkeypatch.setattr(ct, "_udp_totals", lambda: None)                         # không có ổ raw: dùng số đo giao diện
    monkeypatch.setattr(ct, "_net_totals", lambda: {"rx_pkt": 0, "rx_byte": state["rx"], "rx_err": 0, "rx_drop": 0,
                                                    "tx_pkt": 0, "tx_byte": state["tx"], "tx_err": 0, "tx_drop": 0, "udp_drop": 0})
    for i, r in enumerate([0] + list(rx_rates)):
        state["rx"] += r
        state["tx"] += tx_rate
        ct.sample_once(t, i)


def test_signal_that_decays_gradually_is_flagged_bad_and_kept(tmp_path, monkeypatch):
    d = _setup(tmp_path, keep=1)
    ct.line("aaaa000001", "su-kien", "bat dau")
    t = ct._traces["aaaa000001"]
    _feed(monkeypatch, t, [12000] * 10 + [9000, 6000, 4000, 3000, 2500, 2000, 1500, 1000, 800, 600])        # tut dan toi gan het
    txt = _txt(d, "aaaa000001")
    assert "xu-huong nhan=" in txt and "dinh_nhan=" in txt and "danh-dau-loi toc_do_nhan_tut_duoi_60%_dinh" in txt, txt
    assert (d / "aaaa000001.loi").read_text(encoding="utf-8").startswith("toc_do_nhan_tut")


def test_steady_call_is_good_and_old_good_calls_are_deleted_but_bad_ones_stay(tmp_path, monkeypatch):
    d = _setup(tmp_path, keep=1)
    ct.KEEP_BAD = 30
    ct.line("aaaa000010", "su-kien", "cuoc loi")
    ct.mark_bad("aaaa000010", "ntgcalls_failed")
    for i in range(4):                                          # 4 cuoc tot lien tiep
        time.sleep(0.02)
        ct.line("aaaa00002%d" % i, "su-kien", "cuoc tot")
        t = ct._traces["aaaa00002%d" % i]
        _feed(monkeypatch, t, [10000] * 12)
        ct.finish("aaaa00002%d" % i, "xong")
        t.sampler = None
        t.closed = True
        ct._prune()
    names = sorted(os.listdir(d))
    assert "aaaa000010.log" in names and "aaaa000010.loi" in names, names                  # cuoc loi con
    assert [n for n in names if n.endswith(".log") and "aaaa00001" not in n] == ["aaaa000023.log"], names     # chi 1 cuoc tot cuoi
    assert not any(n.startswith("aaaa00002") and n.endswith(".loi") for n in names), names
    ls = {r["id"]: r["loi"] for r in ct.list_traces()}
    assert ls["aaaa000010"] == "ntgcalls_failed" and ls["aaaa000023"] is None


def test_call_ending_while_receive_rate_is_far_below_peak_is_bad(tmp_path, monkeypatch):
    d = _setup(tmp_path, keep=1)
    ct.line("aaaa000030", "su-kien", "bat dau")
    t = ct._traces["aaaa000030"]
    _feed(monkeypatch, t, [12000] * 8 + [500] * 5)                # ket thuc khi toc do chi con ~4% dinh (chua du 5 giay tut -> do finish bat)
    t.low_streak = 0
    ct.finish("aaaa000030", "nguoi dung cup may")
    assert (d / "aaaa000030.loi").exists() and "ket_thuc_khi_toc_do_nhan_con_" in (d / "aaaa000030.loi").read_text(encoding="utf-8")


def test_live_call_marks_ntgcalls_failure_bad(tmp_path):
    rt_calls = {}

    class RT(StubRuntime):
        calls = rt_calls

    d = _setup(tmp_path)
    c = live_call.LiveCall(RT(), "+84", StubClient(), 5, False)
    c.log("bat dau")
    ct.mark_bad(c.id, "ntgcalls_timeout")
    assert (d / (c.id + ".loi")).read_text(encoding="utf-8").strip() == "ntgcalls_timeout"


def test_phone_bitrate_halving_over_30s_like_real_calls_is_flagged(tmp_path, monkeypatch):
    """Số đo thật 18/11 (2 cuộc Telegram): tốc độ nhận 150 -> 57 KB/s dần dần trong ~30 s (còn 38-42% đỉnh) — vạch sóng tụt dần. Phải bị đánh dấu LỖI."""
    d = _setup(tmp_path, keep=1)
    ct.line("aaaa000040", "su-kien", "bat dau")
    t = ct._traces["aaaa000040"]
    _feed(monkeypatch, t, [150000, 150000, 140000, 120000, 105000, 92000, 83000, 74000, 71000, 88000, 58000, 47000, 45000, 44000, 43000, 42000])
    assert (d / "aaaa000040.loi").exists()
    ct.reset_for_tests()


def test_cpu_throttling_of_the_container_is_logged_and_flags_the_call(tmp_path, monkeypatch):
    d = _setup(tmp_path, keep=1)
    ct.line("aaaa000050", "su-kien", "bat dau")
    t = ct._traces["aaaa000050"]
    seq = iter([{"nr_throttled": 0, "throttled_usec": 0, "cpu_usec": 100}, {"nr_throttled": 0, "throttled_usec": 0, "cpu_usec": 200},
                {"nr_throttled": 3, "throttled_usec": 90000, "cpu_usec": 400}])
    monkeypatch.setattr(ct, "_cgroup_cpu", lambda: next(seq))
    for i in range(3):
        ct.sample_once(t, i)
    txt = _txt(d, "aaaa000050")
    assert "cpu-bi-bop" in txt and "nr_throttled+3" in txt and "throttled_usec+90000" in txt and (d / "aaaa000050.loi").read_text(encoding="utf-8").strip() == "container_bi_bop_cpu", txt


def _frame(sport, dport, payload=b"x" * 100):
    ip = bytearray(20); ip[0] = 0x45; ip[9] = 17
    udp = sport.to_bytes(2, "big") + dport.to_bytes(2, "big") + (8 + len(payload)).to_bytes(2, "big") + b"\0\0"
    return b"\0" * 12 + b"\x08\x00" + bytes(ip) + udp + payload


def test_udp_meter_counts_only_udp_media_not_dns_or_tcp_and_splits_direction():
    ct._udp.update({"rx_byte": 0, "tx_byte": 0, "rx_pkt": 0, "tx_pkt": 0})
    assert ct._udp_account(_frame(1400, 40000), 0) and ct._udp_account(_frame(40000, 1400, b"y" * 60), 4)
    assert not ct._udp_account(_frame(53, 40000), 0) and not ct._udp_account(_frame(40000, 53), 4)         # DNS bỏ
    tcp = bytearray(_frame(1400, 40000)); tcp[23] = 6
    assert not ct._udp_account(bytes(tcp), 0) and not ct._udp_account(b"\0" * 20, 0)                       # TCP / khung ngắn bỏ
    assert ct._udp["rx_byte"] == 100 and ct._udp["rx_pkt"] == 1 and ct._udp["tx_byte"] == 60 and ct._udp["tx_pkt"] == 1


def test_trend_uses_udp_meter_when_available_instead_of_whole_interface(tmp_path, monkeypatch):
    """Giao diện chở cả RTSP camera nên «nhận» to giả; xu hướng phải theo UDP (media Telegram)."""
    d = _setup(tmp_path, keep=1)
    ct.line("aaaa000060", "su-kien", "x")
    t = ct._traces["aaaa000060"]
    iface = {"rx": 0}
    udp = {"rx_byte": 0, "tx_byte": 0, "rx_pkt": 0, "tx_pkt": 0}
    monkeypatch.setattr(ct, "_net_totals", lambda: {"rx_pkt": 0, "rx_byte": iface["rx"], "rx_err": 0, "rx_drop": 0, "tx_pkt": 0, "tx_byte": 0, "tx_err": 0, "tx_drop": 0, "udp_drop": 0})
    monkeypatch.setattr(ct, "_udp_totals", lambda: dict(udp))
    for i in range(14):
        iface["rx"] += 500000 if i < 3 else 100000                 # giao diện tụt vì RTSP, UDP vẫn đều
        udp["rx_byte"] += 120000
        ct.sample_once(t, i)
    txt = _txt(d, "aaaa000060")
    assert "udp-flujo-1s" in txt and not (d / "aaaa000060.loi").exists(), txt


def test_udp_meter_measures_gaps_between_packets_for_jitter():
    ct._udp_window_take()
    ct._udp_last.update({"rx": None, "tx": None})
    for ts in (10.000, 10.020, 10.040, 10.200, 10.220):          # khoảng 20, 20, 160, 20 ms
        ct._udp_account(_frame(1400, 40000), 0, ts)
    ct._udp_account(_frame(40000, 1400), 4, 11.0)
    ct._udp_account(_frame(40000, 1400), 4, 11.5)
    w = ct._udp_window_take()
    assert w["rx"][0] == 4 and abs(w["rx"][1] - 160.0) < 1 and w["rx"][2] == 1, w
    assert w["tx"][0] == 1 and abs(w["tx"][1] - 500.0) < 1 and w["tx"][2] == 1, w
    assert ct._udp_window_take()["rx"][0] == 0                                   # đã xoá


def test_on_host_network_only_own_udp_ports_are_counted(monkeypatch):
    """network_mode: host — giao diện chở mọi thứ của máy chủ; chỉ tính gói có cổng nguồn/đích là cổng của chính tiến trình."""
    monkeypatch.setattr(ct, "host_network", lambda: True)
    monkeypatch.setattr(ct, "_own_udp_ports", lambda: {40000})
    ct._own_ports.update({"set": None, "at": 0.0})
    ct._udp.update({"rx_byte": 0, "tx_byte": 0, "rx_pkt": 0, "tx_pkt": 0})
    assert ct._udp_account(_frame(1400, 40000), 0)                         # của ta
    assert not ct._udp_account(_frame(5353, 5353), 0) and not ct._udp_account(_frame(1400, 41234), 0)   # mDNS / tiến trình khác
    assert ct._udp["rx_pkt"] == 1
    monkeypatch.setattr(ct, "host_network", lambda: False)
    assert ct._udp_account(_frame(5353, 5353), 0)                          # bridge: đếm hết (mọi UDP trong container là của gateway)
    ct._own_ports.update({"set": None, "at": 0.0})


def test_own_udp_ports_finds_a_socket_we_opened():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    try:
        assert s.getsockname()[1] in ct._own_udp_ports()
    finally:
        s.close()


def test_rx_video_spec_parses_wxh_at_fps_and_falls_back_on_bad_input():
    assert live_call.parse_rx_video("1280x720@30") == (1280, 720, 30)
    assert live_call.parse_rx_video(" 640x360@15 ") == (640, 360, 15)
    assert live_call.parse_rx_video("640x360") == (640, 360, 15)             # thiếu @fps → mặc định
    assert live_call.parse_rx_video("99999x720@30") == (640, 360, 15)       # ngoài khoảng
    assert live_call.parse_rx_video("abc") == (640, 360, 15) and live_call.parse_rx_video(None) == (640, 360, 15)
    assert live_call.parse_rx_video("bad", default=(1280, 720, 30)) == (1280, 720, 30)
