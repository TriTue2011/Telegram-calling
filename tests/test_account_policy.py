"""Chính sách tài khoản không phụ thuộc HA; đường dịch vụ HA được kiểm riêng."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "telegram_call"
PKG = "telegram_account_policy_test"
pkg = ModuleType(PKG)
pkg.__path__ = [str(ROOT)]
sys.modules[PKG] = pkg
spec = importlib.util.spec_from_file_location(f"{PKG}.accounts", ROOT / "accounts.py")
accounts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = accounts
spec.loader.exec_module(accounts)


def entry(key, default=False, phone="", name=""):
    return SimpleNamespace(entry_id=key, options={"default_account": default}, data={"account": phone},
                           runtime_data=SimpleNamespace(account=SimpleNamespace(data={"display_name": name})))


def test_legacy_services_use_first_loaded_entry_with_two_accounts():
    first, second = entry("old"), entry("new")
    assert accounts.default_entry([first, second]) is first


def test_saved_default_survives_changed_load_order_and_unavailable_entry():
    first, second = entry("old", True), entry("new")
    assert accounts.default_entry([second, first]) is first
    assert accounts.default_entry([second]) is second
    assert accounts.default_entry([]) is None


def test_options_can_switch_the_default_account():
    first, second = entry("old"), entry("new", True)
    assert accounts.default_entry([first, second]) is second


def test_service_selection_priority_phone_name_and_unknown_account():
    first = entry("old", True, "+84900000000", "Nhà")
    second = entry("new", False, "+84900000001", "Cơ quan")
    entries = [first, second]
    assert accounts.select_entry(entries) is first
    assert accounts.select_entry(entries, account="+84900000001") is second
    assert accounts.select_entry(entries, account="  CƠ QUAN  ") is second
    assert accounts.select_entry(entries, entry_id="old", account="không có") is first
    with pytest.raises(ValueError, match="Không tìm thấy tài khoản"):
        accounts.select_entry(entries, account="không có")
    with pytest.raises(ValueError, match="Không có mục cấu hình"):
        accounts.select_entry(entries, entry_id="không có", account="Nhà")
    with pytest.raises(ValueError, match="Chưa có tài khoản"):
        accounts.select_entry([])


def test_duplicate_display_names_require_an_unambiguous_selector():
    entries = [entry("a", phone="+84900000000", name="Nhà"), entry("b", phone="+84900000001", name="Nhà")]
    with pytest.raises(ValueError, match="Có nhiều tài khoản"):
        accounts.select_entry(entries, account="Nhà")
    assert accounts.select_entry(entries, account="+84900000001") is entries[1]


def test_device_name_follows_profile_and_masks_phone_fallback():
    assert accounts.device_name("+84900000000", "Nhà") == "Telegram Call — Nhà"
    assert accounts.device_name("+84900000000", "Tên mới") == "Telegram Call — Tên mới"
    assert accounts.device_name("+84900000000") == "Telegram Call — +84…000"
    assert "+84900000000" not in accounts.device_name("+84900000000")
