"""Che bí mật khỏi log — kỷ luật bắt buộc của gateway.

Mọi giá trị nhạy cảm (token, mã OTP/2FA, api_id/api_hash, khóa session,
nội dung mã đăng nhập) đều bị thay bằng "••••" trước khi ghi log.
Không bao giờ ghi toàn bộ số điện thoại vào log ở mức INFO (chỉ che một
phần khi thật cần thiết).
"""

import logging
import re

_PLACEHOLDER = "••••"
_PATTERNS = [
    re.compile(r"(?i)(token|api_hash|api_id|password|otp|code|session)[\"']?\s*[:=]\s*[\"']?[^\"',}\s]+"),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
]


class RedactingFilter(logging.Filter):
    """Lọc log: thay các mẫu nhạy cảm bằng placeholder."""

    def __init__(self, extra_secrets: tuple[str, ...] = ()):
        super().__init__()
        self._secrets = [s for s in extra_secrets if s]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        for pat in _PATTERNS:
            msg = pat.sub(lambda m: m.group(0).split(":")[0].split("=")[0] + f"={_PLACEHOLDER}"
                          if ("=" in m.group(0) or ":" in m.group(0)) else _PLACEHOLDER, msg)
        for secret in self._secrets:
            msg = msg.replace(secret, _PLACEHOLDER)
        record.msg = msg
        record.args = ()
        return True


def mask_peer(peer: str) -> str:
    """Che một phần peer (username/số) để log không lộ danh tính đầy đủ."""
    if not peer:
        return ""
    if len(peer) <= 4:
        return "••••"
    return peer[:2] + "••••" + peer[-2:]


def setup_logging(level: str, secrets: tuple[str, ...] = ()) -> logging.Logger:
    logger = logging.getLogger("telegram_gateway")
    logger.setLevel(level)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter(secrets))
    logger.handlers = [handler]
    logger.propagate = False
    return logger
