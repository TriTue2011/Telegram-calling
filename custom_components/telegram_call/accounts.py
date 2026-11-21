"""Chọn mục mặc định ổn định cho các lời gọi dịch vụ cũ."""
from __future__ import annotations

from .const import CONF_DEFAULT


def default_entry(entries):
    """Ưu tiên mục đã lưu mặc định; khi mục ấy chưa nạp, dùng mục nạp đầu tiên."""
    return next((e for e in entries if e.options.get(CONF_DEFAULT)), entries[0] if entries else None)
