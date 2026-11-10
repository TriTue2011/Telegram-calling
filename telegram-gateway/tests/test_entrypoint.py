"""Entrypoint add-on: tuỳ chọn api_id/api_hash/token thành biến môi trường của gateway."""
import importlib.util
import json
import os
import stat

spec = importlib.util.spec_from_file_location(
    "docker_entrypoint", os.path.join(os.path.dirname(__file__), "..", "docker-entrypoint.py"))
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)


def _opts(tmp_path, data):
    p = tmp_path / "options.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


def test_khong_co_options_thi_khong_dong_vao_gi(tmp_path):
    env = {}
    assert ep.apply_addon_options(env, str(tmp_path / "khong-co.json"), str(tmp_path)) is None
    (tmp_path / "hong.json").write_text("{ khong phai json", encoding="utf-8")
    assert ep.apply_addon_options(env, str(tmp_path / "hong.json"), str(tmp_path)) is None
    assert env == {}


def test_co_api_id_hash_thi_bat_engine_that(tmp_path):
    env = {}
    info = ep.apply_addon_options(env, _opts(tmp_path, {
        "api_id": " 12345 ", "api_hash": "abcdef", "gateway_token": "tok-nguoi-dung",
        "ha_webhook_url": "http://ha:8123/api/webhook/x"}), str(tmp_path))
    assert info == {"token_moi": None, "mock": False}
    assert env == {"GATEWAY_TOKEN": "tok-nguoi-dung", "TELEGRAM_API_ID": "12345",
                   "TELEGRAM_API_HASH": "abcdef", "GATEWAY_MOCK": "0",
                   "HA_WEBHOOK_URL": "http://ha:8123/api/webhook/x"}


def test_thieu_api_hash_thi_van_chay_gia_lap_va_bao_ro(tmp_path):
    env = {}
    info = ep.apply_addon_options(env, _opts(tmp_path, {"api_id": "12345", "api_hash": ""}), str(tmp_path))
    assert info["mock"] is True and env["GATEWAY_MOCK"] == "1" and "TELEGRAM_API_ID" not in env


def test_bo_trong_token_thi_tu_sinh_giu_lai_va_khong_doi(tmp_path):
    opts = _opts(tmp_path, {"api_id": "", "api_hash": "", "gateway_token": ""})
    env1 = {}
    t1 = ep.apply_addon_options(env1, opts, str(tmp_path))["token_moi"]
    assert t1 and len(t1) == 64 and env1["GATEWAY_TOKEN"] == t1
    assert stat.S_IMODE(os.stat(tmp_path / ep.TOKEN_NAME).st_mode) == 0o600
    env2 = {}
    assert ep.apply_addon_options(env2, opts, str(tmp_path))["token_moi"] == t1      # khởi động lại: cùng token


def test_bien_moi_truong_dat_san_thang_tuy_chon(tmp_path):
    env = {"GATEWAY_TOKEN": "tu-env", "GATEWAY_MOCK": "0"}
    ep.apply_addon_options(env, _opts(tmp_path, {"gateway_token": "khac"}), str(tmp_path))
    assert env["GATEWAY_TOKEN"] == "tu-env" and env["GATEWAY_MOCK"] == "0"


def test_video_profile_tu_tuy_chon_addon_chi_nhan_gia_tri_hop_le(tmp_path):
    env = {}
    ep.apply_addon_options(env, _opts(tmp_path, {"video_profile": "LOW"}), str(tmp_path))
    assert env["VIDEO_PROFILE"] == "low"
    env = {}
    ep.apply_addon_options(env, _opts(tmp_path, {"video_profile": "ultra-nang"}), str(tmp_path))
    assert "VIDEO_PROFILE" not in env
