"""Cấu trúc tích hợp telegram_call: manifest, bản dịch khớp strings, services.yaml hợp lệ, hacs.json (HACS: một tích hợp mỗi kho)."""
import glob
import json
import os

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
INT = os.path.join(ROOT, "custom_components", "telegram_call")


def _json(*p):
    with open(os.path.join(*p), encoding="utf-8") as fh:
        return json.load(fh)


def test_hacs_mot_tich_hop_moi_kho():
    dirs = [d for d in glob.glob(os.path.join(ROOT, "custom_components", "*")) if os.path.isdir(d) and not d.endswith("__pycache__")]
    assert len(dirs) == 1 and os.path.basename(dirs[0]) == "telegram_call", dirs
    h = _json(ROOT, "hacs.json")
    assert h["name"] and h.get("render_readme") is True


def test_manifest_hop_le():
    m = _json(INT, "manifest.json")
    for k in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners", "config_flow"):
        assert k in m, k
    assert m["domain"] == "telegram_call" and m["config_flow"] is True
    assert "Telegram-calling" in m["documentation"] and "Telegram-calling" in m["issue_tracker"]
    assert m["version"].count(".") == 2


def test_ban_dich_khop_strings():
    s = _json(INT, "strings.json")
    for lang in ("en", "vi"):
        t = _json(INT, "translations", lang + ".json")
        assert set(t.get("config", {}).get("step", {})) == set(s.get("config", {}).get("step", {})), lang
        assert set(t.get("config", {}).get("error", {})) == set(s.get("config", {}).get("error", {})), lang


def test_services_yaml_hop_le():
    with open(os.path.join(INT, "services.yaml"), encoding="utf-8") as fh:
        d = yaml.safe_load(fh)
    assert isinstance(d, dict) and d
    for name, svc in d.items():
        assert isinstance(svc, dict), name


def test_gateway_version_khop_manifest():
    with open(os.path.join(ROOT, "telegram-gateway", "VERSION"), encoding="utf-8") as fh:
        gv = fh.read().strip()
    assert gv == _json(INT, "manifest.json")["version"], "VERSION của gateway phải bằng version tích hợp (một thẻ phát hành cho cả hai)"


def test_lich_cap_nhat_trang_thai_phai_la_callback():
    """HA chạy hàm thường ở luồng nền; async_write_ha_state từ đó gây lỗi mỗi phút (đo 10/11/2026 ở zalo_call).
    Không được truyền lambda gọi async_write_ha_state cho async_track_*."""
    import ast
    bad = []
    for path in sorted(glob.glob(os.path.join(INT, "*.py"))):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for call in ast.walk(tree):
            name = getattr(call.func, "id", getattr(call.func, "attr", "")) if isinstance(call, ast.Call) else ""
            if not name.startswith("async_track_"):
                continue
            for arg in call.args:
                if isinstance(arg, ast.Lambda) and any(getattr(n, "attr", "") == "async_write_ha_state" for n in ast.walk(arg)):
                    bad.append("%s:%d" % (os.path.basename(path), arg.lineno))
    assert not bad, "lambda gọi async_write_ha_state sẽ chạy ở luồng nền: %s" % bad
