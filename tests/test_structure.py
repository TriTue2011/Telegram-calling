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
    assert m["version"].count(".") == 3 and all(p.isdigit() for p in m["version"].split(".")), m["version"]      # AAAA.MM.NN.N như Zalo-calling


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


def test_moi_dich_vu_co_bo_chon_tai_khoan_va_ban_dich():
    with open(os.path.join(INT, "services.yaml"), encoding="utf-8") as fh:
        services = yaml.safe_load(fh)
    for name, svc in services.items():
        assert {"config_entry_id", "account"} <= svc["fields"].keys(), name
        for rel in ("strings.json", "translations/vi.json", "translations/en.json"):
            fields = _json(INT, *rel.split("/"))["services"][name]["fields"]
            assert fields["account"]["name"] and fields["account"]["description"], (rel, name)


def test_gateway_version_khop_manifest():
    with open(os.path.join(ROOT, "telegram-gateway", "VERSION"), encoding="utf-8") as fh:
        gv = fh.read().strip()
    assert gv == _json(INT, "manifest.json")["version"], "VERSION của gateway phải bằng version tích hợp (một thẻ phát hành cho cả hai)"


def test_readme_huong_dan_nhieu_tai_khoan_va_automation_chon_tai_khoan():
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
        text = fh.read()
    assert "## Nhiều tài khoản" in text
    assert "Thêm một mục cấu hình cho từng tài khoản" in text
    assert "config_entry_id` → `account` → mặc định" in text
    assert 'account: "{{ trigger.event.data.account }}"' in text
    assert 'account: "+84900000001"' in text


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


def test_khong_boc_callback_len_phuong_thuc_da_gan():
    """`callback(self.async_write_ha_state)` làm HA mới (Python 3.14) báo «'method' object has no attribute '_hass_callback'» và THỰC THỂ KHÔNG ĐƯỢC THÊM
    (cảm biến/sự kiện biến mất, không tạo được automation; đo 10–15/11/2026 ở telegram_call 0.4.1). Phương thức của HA đã là callback sẵn:
    truyền thẳng; chỉ hàm thường/lambda mới được bọc bằng callback()."""
    import ast
    bad = []
    for path in sorted(glob.glob(os.path.join(INT, "*.py"))):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for call in ast.walk(tree):
            if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "callback":
                for arg in call.args:
                    if isinstance(arg, ast.Attribute):
                        bad.append("%s:%d" % (os.path.basename(path), arg.lineno))
    assert not bad, "callback() bọc lên phương thức đã gắn: %s" % bad


def test_catalog_dich_vu_khop_services_yaml_va_ban_dich():
    """catalog.py là nguồn duy nhất: services.yaml và strings/vi/en phải có đủ dịch vụ và đủ ô của nó (chạy ``python3 tools/gen_catalog.py`` nếu lệch)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("telegram_call_catalog", os.path.join(INT, "catalog.py"))
    cat = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = cat
    spec.loader.exec_module(cat)
    with open(os.path.join(INT, "services.yaml"), encoding="utf-8") as fh:
        yml = yaml.safe_load(fh)
    texts = {rel: _json(INT, *rel.split("/"))["services"] for rel in ("strings.json", "translations/vi.json", "translations/en.json")}
    assert len(cat.SERVICES) == 30
    for name, svc in cat.SERVICES.items():
        own = {f.key for f in svc.fields}
        assert own <= set(yml[name]["fields"]), name
        for rel, t in texts.items():
            assert name in t and t[name]["name"] and t[name]["description"], (rel, name)
            assert set(t[name]["fields"]) == set(yml[name]["fields"]), (rel, name)
            for f in svc.fields:
                assert t[name]["fields"][f.key]["name"], (rel, name, f.key)
