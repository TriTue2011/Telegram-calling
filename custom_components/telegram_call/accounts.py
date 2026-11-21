"""Chọn mục mặc định ổn định cho các lời gọi dịch vụ cũ."""
from __future__ import annotations

from .const import CONF_ACCOUNT, CONF_DEFAULT


def default_entry(entries):
    """Ưu tiên mục đã lưu mặc định; khi mục ấy chưa nạp, dùng mục nạp đầu tiên."""
    return next((e for e in entries if e.options.get(CONF_DEFAULT)), entries[0] if entries else None)


def select_entry(entries, entry_id="", account=""):
    """config_entry_id > số/tên tài khoản > mặc định; lựa chọn lạ/trùng tên phải báo lỗi."""
    if entry_id:
        match = next((e for e in entries if e.entry_id == entry_id), None)
        if match is None:
            raise ValueError(f"Không có mục cấu hình {entry_id} được nạp")
        return match
    if account:
        want = account.strip().casefold()
        matches = [e for e in entries if want and want in {
            str(e.data.get(CONF_ACCOUNT) or "").casefold(),
            str((e.runtime_data.account.data or {}).get("display_name") or "").strip().casefold(),
        }]
        if not matches:
            raise ValueError(f"Không tìm thấy tài khoản Telegram Call «{account}» được nạp")
        if len(matches) > 1:
            raise ValueError(f"Có nhiều tài khoản Telegram Call khớp «{account}»: hãy dùng số điện thoại hoặc config_entry_id")
        return matches[0]
    match = default_entry(entries)
    if match is None:
        raise ValueError("Chưa có tài khoản Telegram Call nào được nạp")
    return match
