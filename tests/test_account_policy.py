"""Chính sách tài khoản không phụ thuộc HA; đường dịch vụ HA được kiểm riêng."""
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace


ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "telegram_call"
PKG = "telegram_account_policy_test"
pkg = ModuleType(PKG)
pkg.__path__ = [str(ROOT)]
sys.modules[PKG] = pkg
spec = importlib.util.spec_from_file_location(f"{PKG}.accounts", ROOT / "accounts.py")
accounts = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = accounts
spec.loader.exec_module(accounts)


def entry(key, default=False):
    return SimpleNamespace(entry_id=key, options={"default_account": default})


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
