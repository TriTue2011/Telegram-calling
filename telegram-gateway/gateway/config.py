"""Cấu hình gateway từ biến môi trường (file .env trên server).

Không có giá trị mặc định nào là bí mật thật. GATEWAY_TOKEN bắt buộc phải
đặt trên server (sinh bằng ``openssl rand -hex 32``); nếu thiếu, gateway
từ chối khởi động thay vì tự sinh token rồi giấu đi.
"""

import os
from dataclasses import dataclass, field


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    data_dir: str = os.environ.get("DATA_DIR", "/data")
    gateway_token: str = os.environ.get("GATEWAY_TOKEN", "")
    ha_webhook_url: str = os.environ.get("HA_WEBHOOK_URL", "")
    ha_webhook_token: str = os.environ.get("HA_WEBHOOK_TOKEN", "")
    rest_port: int = _int("REST_PORT", 3333)
    log_level: str = os.environ.get("LOG_LEVEL", "INFO")
    ring_timeout: int = _int("RING_TIMEOUT", 45)
    ring_attempts: int = _int("RING_ATTEMPTS", 2)
    ring_interval: int = _int("RING_INTERVAL", 15)
    max_call_seconds: int = _int("MAX_CALL_SECONDS", 180)
    queue_timeout: int = _int("QUEUE_TIMEOUT", 120)
    queue_depth: int = _int("QUEUE_DEPTH", 10)
    tts_voice: str = os.environ.get("TTS_VOICE", "vi-VN-HoaiMyNeural")
    tts_rate: str = os.environ.get("TTS_RATE", "+0%")
    # API credentials TDLib do chủ máy tạo ở my.telegram.org; chỉ dùng
    # khi bật engine thật (GATEWAY_MOCK=0). Không có giá trị mặc định.
    api_id: str = os.environ.get("TELEGRAM_API_ID", "")
    api_hash: str = os.environ.get("TELEGRAM_API_HASH", "")
    rate_limit_per_minute: int = _int("RATE_LIMIT_PER_MINUTE", 120)
    version: str = "0.1.0"
    # Chế độ mock: True = mọi tác vụ cần tài khoản thật trả mock/501 theo
    # bảng giai đoạn. Bước P1 bật engine thật thì đặt GATEWAY_MOCK=0.
    mock_mode: bool = os.environ.get("GATEWAY_MOCK", "1") != "0"
    event_retention_days: int = 7
    event_max: int = 10000


def load_config() -> Config:
    cfg = Config()
    if not cfg.gateway_token:
        raise RuntimeError(
            "GATEWAY_TOKEN chưa đặt. Sinh trên server bằng "
            "'openssl rand -hex 32' rồi ghi vào .env (0600)."
        )
    return cfg
