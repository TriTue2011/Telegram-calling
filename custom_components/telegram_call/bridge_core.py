"""Phần thuần của cầu nối cuộc gọi — KHÔNG import Home Assistant, nên kiểm được ngoài HA.

Cùng ý với ``zalo_call/bridge_core.py`` (chép lại có chủ ý: HACS chỉ cài một tích hợp cho mỗi kho và hai tích hợp
không được phụ thuộc nhau); ở đây thêm ``derive_state`` cho trạng thái cuộc gọi của gateway Telegram.
"""
from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import quote, urlsplit, urlunsplit

ALLOWED_SCHEMES = ("rtsp", "rtsps", "http", "https", "rtmp", "rtmps")
DEVICE_KEYS = ("mic", "mic_url", "camera", "speaker")
LABEL_SEP = " · "
R1_MIC_PATH = "/api/r1_speaker/mic/"
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0")

#: Trạng thái cho automation, dùng CÙNG tên với ``zalo_call_call_state`` để một automation chạy cho cả hai.
STATES = ("incoming", "ringing", "connecting", "answered", "ended", "not_answered", "missed", "error")


def derive_state(info: Mapping[str, Any], was_active: bool) -> str | None:
    """Trạng thái cuộc gọi cho automation từ ``info`` của gateway (``GET /api/live/calls``).

    Gateway chỉ có dialing / ringing / connecting / active / ended (+ ``error``). Nó KHÔNG phân biệt «bên kia từ chối» với
    «không nghe máy» nên cuộc gọi đi chưa nghe máy mà kết thúc là ``not_answered`` (đừng suy diễn thêm)."""
    state = str(info.get("state") or "")
    outgoing = bool(info.get("outgoing"))
    if state in ("dialing", "ringing"):
        return "ringing" if outgoing else "incoming"
    if state == "connecting":
        return "connecting"
    if state == "active":
        return "answered"
    if state == "ended":
        if was_active:
            return "ended"
        if info.get("error"):
            return "error"
        return "not_answered" if outgoing else "missed"
    return None


def pick_devices(defaults: Mapping[str, Any], override: Mapping[str, Any] | None = None) -> dict[str, str]:
    """Thiết bị cho một cuộc gọi: ghi đè > mặc định > không có (chuỗi RỖNG ghi đè = chủ động không dùng)."""
    override = override or {}
    out: dict[str, str] = {}
    for key in DEVICE_KEYS:
        if key in override and override[key] is not None:
            out[key] = str(override[key]).strip()
        else:
            out[key] = str(defaults.get(key) or "").strip()
    return out


def label(entity_id: str, friendly: str | None) -> str:
    """Nhãn hiện trên ô chọn: «Tên · entity_id» (hoặc entity_id nếu không có tên)."""
    name = (friendly or "").strip()
    return f"{name}{LABEL_SEP}{entity_id}" if name and name != entity_id else entity_id


def entity_of_label(text: str) -> str:
    """entity_id từ nhãn (phần sau dấu « · » cuối cùng)."""
    return (text or "").rsplit(LABEL_SEP, 1)[-1].strip()


def valid_url(url: str) -> bool:
    parts = urlsplit(url or "")
    return parts.scheme.lower() in ALLOWED_SCHEMES and bool(parts.netloc) and " " not in url and "\n" not in url


def host_of(base_url: str) -> str:
    return urlsplit(base_url or "").hostname or ""


def localize_url(url: str, host: str) -> str:
    """Đổi địa chỉ máy CỤC BỘ (localhost, 127.0.0.1…) trong ``url`` thành ``host``: gateway chạy trong container nên
    ``localhost`` của HA không tới được từ nó."""
    parts = urlsplit(url or "")
    if not host or (parts.hostname or "").lower() not in LOOPBACK_HOSTS:
        return url
    userinfo = parts.netloc.rpartition("@")[0]
    netloc = (userinfo + "@" if userinfo else "") + host + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def mic_format_for_url(url: str) -> str:
    """``s16le16k`` cho luồng PCM thô của mic loa R1, còn lại ``auto``."""
    return "s16le16k" if R1_MIC_PATH in (url or "") else "auto"


def r1_mic_url(base: str, entry_id: str, key: str) -> str:
    return f"{base.rstrip('/')}{R1_MIC_PATH}{quote(entry_id, safe='')}?k={quote(key, safe='')}"


def intercom_url(base: str, domain: str, entry_id: str, key: str, kind: str = "intercom") -> str:
    """Cửa bộ đàm của loa (``r1_speaker`` / ``dahua_talk``): gateway POST tiếng bên kia vào đây. Khớp ``valid_speaker_url`` của gateway."""
    return f"{base.rstrip('/')}/api/{domain}/{kind}/{quote(entry_id, safe='')}?k={quote(key, safe='')}"


def dahua_mic_url(options: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    return str(options.get("mic_url") or data.get("mic_url") or "").strip()


_SECRET = re.compile(r"([?&]k=)[^&\s]+|(://[^/\s:@]+:)[^@/\s]*@")


def redact(text: str) -> str:
    """Che khoá ``?k=…`` và mật khẩu ``user:pass@`` trong một chuỗi trước khi đưa vào log / lỗi / sự kiện."""
    return _SECRET.sub(lambda m: (m.group(1) + "***") if m.group(1) else (m.group(2) + "***@"), text or "")


class PushPolicy:
    """Khi nào phải đưa lại thiết bị mặc định cho gateway: chưa đưa được lần nào / lần trước lỗi, hoặc gateway vừa sống lại."""

    def __init__(self) -> None:
        self.pushed_ok = False
        self._down = False

    def poll(self, ok: bool) -> bool:
        if not ok:
            self._down = True
            return False
        need = self._down or not self.pushed_ok
        self._down = False
        return need

    def pushed(self, ok: bool) -> None:
        self.pushed_ok = bool(ok)
