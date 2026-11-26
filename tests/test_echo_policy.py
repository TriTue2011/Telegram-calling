"""Kiểm thử thuần Python cho ``echo_policy.py`` (không cần Home Assistant).

Chạy trực tiếp: ``python3 tests/test_echo_policy.py`` hoặc qua pytest."""
import importlib.util
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "telegram_call")


def _load_module(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


echo_policy = _load_module("echo_policy")


def test_echo_modes_contains_all_four_modes():
    assert echo_policy.ECHO_MODES == ("auto", "on", "duplex", "off")


def test_rule_explicit_mode_always_wins_over_device_default():
    for rule_mode in ("on", "duplex", "off"):
        for device_default in (*echo_policy.ECHO_MODES, None, "", "invalid", 123):
            assert echo_policy.effective(rule_mode, device_default) == rule_mode, (rule_mode, device_default)


def test_rule_auto_empty_or_invalid_falls_back_to_device_default():
    for rule_mode in ("auto", None, "", "la-hoac", 0, False):
        for device_default in echo_policy.ECHO_MODES:
            assert echo_policy.effective(rule_mode, device_default) == device_default, (rule_mode, device_default)


def test_both_invalid_or_missing_fall_back_to_auto():
    for rule_mode in ("auto", None, "", "bad"):
        for device_default in (None, "", "bad", 42, True):
            assert echo_policy.effective(rule_mode, device_default) == "auto", (rule_mode, device_default)


def test_effective_is_idempotent():
    for rule_mode in (*echo_policy.ECHO_MODES, None, "", "bad"):
        for device_default in (*echo_policy.ECHO_MODES, None, "", "bad"):
            first = echo_policy.effective(rule_mode, device_default)
            assert first in echo_policy.ECHO_MODES
            assert echo_policy.effective(first, device_default) == first


if __name__ == "__main__":
    for _name, _fn in list(globals().items()):
        if _name.startswith("test_"):
            _fn()
            print("PASS", _name)
    print("ALL ECHO POLICY TESTS PASSED")
