"""Công tắc nhật ký WebRTC/NTgCalls: tắt mặc định, bật ghi tệp riêng 0600, lọc theo regex, không đổ vào nhật ký chính. Không mạng."""
import logging
import os
import stat

import pytest

from gateway import webrtc_log


@pytest.fixture(autouse=True)
def _tra_logger():
    cu = {t: (logging.getLogger(t).level, list(logging.getLogger(t).handlers), logging.getLogger(t).propagate)
          for t in webrtc_log._TEN}
    yield
    for t, (muc, hs, pr) in cu.items():
        lg = logging.getLogger(t)
        for h in lg.handlers:
            if h not in hs:
                h.close()
        lg.handlers[:] = hs
        lg.setLevel(muc)
        lg.propagate = pr


def test_tat_mac_dinh_khong_dong_logger(tmp_path):
    for env in ({}, {"TELEGRAM_WEBRTC_LOG": ""}, {"TELEGRAM_WEBRTC_LOG": "0"}, {"TELEGRAM_WEBRTC_LOG": "verbose"}):
        assert webrtc_log.bat(str(tmp_path), env) is None
    assert not (tmp_path / "calltrace").exists()
    assert logging.getLogger("webrtc").handlers == []


def test_bat_debug_ghi_tep_rieng_0600_khong_lan_sang_nhat_ky_chinh(tmp_path, caplog):
    duong = webrtc_log.bat(str(tmp_path), {"TELEGRAM_WEBRTC_LOG": "debug"})
    assert duong == os.path.join(str(tmp_path), "calltrace", "webrtc.log")
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("webrtc").debug("delay_based_bwe.cc:200 BWE overuse")
        logging.getLogger("ntgcalls").info("p2p_call.cpp:10 connected")
    for h in logging.getLogger("webrtc").handlers:
        h.flush()
    noi_dung = open(duong, encoding="utf-8").read()
    assert "webrtc DEBUG delay_based_bwe.cc:200 BWE overuse" in noi_dung
    assert "ntgcalls INFO p2p_call.cpp:10 connected" in noi_dung
    assert stat.S_IMODE(os.stat(duong).st_mode) == 0o600
    assert "BWE overuse" not in caplog.text                     # propagate=False: không đổ vào docker logs


def test_muc_info_bo_debug_va_loc_regex(tmp_path):
    duong = webrtc_log.bat(str(tmp_path), {"TELEGRAM_WEBRTC_LOG": "INFO", "TELEGRAM_WEBRTC_LOG_LOC": "goog_cc|rtcp"})
    lg = logging.getLogger("webrtc")
    lg.debug("goog_cc.cc:1 bo vi muc debug")
    lg.info("goog_cc.cc:2 giu lai")
    lg.info("video_encoder.cc:3 bo vi khong khop loc")
    for h in lg.handlers:
        h.flush()
    noi_dung = open(duong, encoding="utf-8").read()
    assert "giu lai" in noi_dung
    assert "bo vi muc debug" not in noi_dung and "khong khop loc" not in noi_dung
