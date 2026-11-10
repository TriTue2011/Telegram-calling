"""Hằng số của tích hợp Telegram Call."""

DOMAIN = "telegram_call"

CONF_URL = "url"                  # địa chỉ gateway (telegram-gateway), ví dụ http://172.16.10.200:3333
CONF_TOKEN = "token"              # GATEWAY_TOKEN của gateway (gửi dạng Authorization: Bearer)
CONF_ACCOUNT = "account"          # số điện thoại tài khoản Telegram trên gateway

CONF_MIC = "mic"                  # nhãn «Tên · entity_id» của nguồn mic (camera / vệ tinh Assist)
CONF_MIC_URL = "mic_url"          # URL mic bất kỳ (rtsp/http…), thay cho CONF_MIC
CONF_CAMERA = "camera"            # camera cho cuộc gọi video
CONF_SPEAKER = "speaker"          # nhãn «Tên · entity_id» của loa phát tiếng bên kia (loa R1 / camera Imou-Dahua qua cửa bộ đàm)
CONF_CALL_VOLUME = "call_volume"  # % âm lượng loa lúc đang gọi (0 = giữ nguyên âm lượng của loa)
MAX_CALL_VOLUME = 100
CONF_HA_URL = "ha_url"            # địa chỉ HA mà gateway (container) gọi tới được; trống = URL nội bộ của HA

EVENT_STATE = "telegram_call_call_state"

STORAGE_VERSION = 1
NONE_LABEL = "Không"
MIC_URL_LABEL = "URL (ô Mic URL)"

POLL_IDLE_S = 5                   # gateway giới hạn 120 yêu cầu/phút/token (dùng chung với trang web)
POLL_BUSY_S = 2                   # có cuộc gọi đang diễn ra: hỏi dày để báo trạng thái kịp
