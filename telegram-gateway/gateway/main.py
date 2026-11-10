"""Điểm vào gateway: uvicorn gateway.main:app.

Trong container, app phải nghe 0.0.0.0 (Docker forward về IP của
container, không phải loopback của container) — LAN-only được thực thi
ở tầng compose bằng bind phía host 127.0.0.1:3333 (mặc định), hoặc
IP LAN cụ thể + firewall khi HA ở máy khác. App không tự mở cổng ra
Internet; đừng đổi compose bind thành 0.0.0.0 không kèm firewall.
"""

import os

from fastapi.staticfiles import StaticFiles

from .config import load_config
from .redact import setup_logging
from .routes import build_app
from .tdclient import TdlibClient
from .tts import EdgeTtsSynthesizer

cfg = load_config()
logger = setup_logging(cfg.log_level, secrets=(cfg.gateway_token,
                                               cfg.ha_webhook_token))

# Engine thật chỉ bật khi GATEWAY_MOCK=0 và có api_id/api_hash (chủ máy
# tự tạo ở my.telegram.org, nhập qua web UI nội bộ hoặc .env). Mặc định
# mock: gateway chạy khung, login/gửi thật chưa bật, không credential.
_tdlib_factory = None
if not cfg.mock_mode and cfg.api_id and cfg.api_hash:
    def _factory(phone: str, session_dir: str):
        return TdlibClient(session_dir, int(cfg.api_id), cfg.api_hash)
    _tdlib_factory = _factory

app = build_app(cfg, tdlib_factory=_tdlib_factory,
                synthesizer=EdgeTtsSynthesizer(cfg.tts_voice, cfg.tts_rate))

_web_dir = os.path.join(os.path.dirname(__file__), "web")
if os.path.isdir(_web_dir):
    app.mount("/ui", StaticFiles(directory=_web_dir, html=True),
              name="webui")


def main() -> None:
    import uvicorn
    uvicorn.run(app, host=os.environ.get("GATEWAY_HOST", "0.0.0.0"),
                port=cfg.rest_port)


if __name__ == "__main__":
    main()
