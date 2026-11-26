"""Chọn chế độ chống vọng (echo_guard) cho một cuộc gọi: quy tắc của người đó thắng, không thì theo mặc định của thiết bị (thuần Python để test riêng).

Quy tắc để ``auto`` nghĩa là «không riêng»: theo mặc định của thiết bị."""
from __future__ import annotations

from typing import Any

ECHO_MODES = ("auto", "on", "duplex", "off")


def effective(rule_mode: Any, device_default: Any) -> str:
    """on | duplex | off của quy tắc thắng; auto/trống/sai thì theo mặc định thiết bị (sai nữa thì auto)."""
    if rule_mode in ("on", "duplex", "off"):
        return str(rule_mode)
    return str(device_default) if device_default in ECHO_MODES else "auto"
