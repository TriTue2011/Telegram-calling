# ✈️ Telegram Calling

Cổng **Telegram** cho Home Assistant: đăng nhập **tài khoản Telegram phụ**, nhắn tin, gọi thoại/video, đẩy sự kiện về HA.
Gồm **gateway** (Docker, TDLib + NTgCalls) và **tích hợp HA** `telegram_call` (HACS).

> ⚠️ **Dùng tài khoản phụ**, không dùng tài khoản chính. Một tài khoản chỉ đăng nhập ở **một nơi**.
> ⚠️ **Bản xem trước (0.x):** gateway chạy và có test; tích hợp HA đang hoàn thiện, gọi thật chưa kiểm hết.
> Zalo: kho riêng [Zalo-calling](https://github.com/TriTue2011/Zalo-calling).

## Thêm nhanh vào Home Assistant

| Bước | Nút |
|---|---|
| ① Thêm kho add-on (HAOS / Supervised) | [![Thêm kho add-on](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FTriTue2011%2Fhas-addons) |
| ② Mở tích hợp trong HACS | [![Mở trong HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=TriTue2011&repository=Telegram-calling&category=integration) |
| ③ Thêm tích hợp (sau khi tải + khởi động lại HA) | [![Thêm tích hợp](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=telegram_call) |

**Làm tay (HACS 2.x):** HACS → **⋮ → Custom repositories** → dán `https://github.com/TriTue2011/Telegram-calling`,
**Type = Integration** → **Add** → tìm **Telegram Call** → **Download** → khởi động lại HA.

## 1. Lấy thông số Telegram (một lần)

Cần **`api_id`** và **`api_hash`**:

1. Vào <https://my.telegram.org>, nhập số điện thoại (tài khoản phụ) → nhập mã Telegram gửi về.
2. Chọn **API development tools**.
3. Điền **App title**, **Short name** (tuỳ ý), **Platform** = *Desktop* → **Create application**.
4. Chép **App api_id** (một số) và **App api_hash** (chuỗi 32 ký tự). **Giữ bí mật `api_hash`.**

## 2. Đặt thông số

**Add-on** (HAOS / Supervised): cài **Telegram Calling** từ kho `has-addons` → tab **Cấu hình**:

| Tuỳ chọn | Điền gì |
|---|---|
| `api_id` / `api_hash` | Hai giá trị ở bước 1 |
| `gateway_token` | Bỏ trống = tự sinh, in ra tab **Nhật ký** |

**Docker** — `docker-compose.yml` + `.env` cùng thư mục:

```yaml
services:
  telegram-calling:
    image: ghcr.io/tritue2011/telegram-calling:latest
    container_name: telegram-calling
    restart: unless-stopped
    ports:
      - "127.0.0.1:3333:3333"   # HA ở máy khác: "IP_LAN:3333:3333" + firewall chỉ cho IP của HA
    volumes:
      - ./data:/data            # phiên Telegram nằm ở đây, giữ kín
    environment:
      GATEWAY_TOKEN: ${GATEWAY_TOKEN}
      TELEGRAM_API_ID: ${TELEGRAM_API_ID}
      TELEGRAM_API_HASH: ${TELEGRAM_API_HASH}
      GATEWAY_MOCK: "0"         # 0 = chạy thật; 1 = giả lập
```

```
# .env (chmod 600). GATEWAY_TOKEN sinh bằng: openssl rand -hex 32
GATEWAY_TOKEN=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
```

`docker compose up -d` → mở `http://<máy>:3333/ui/`, dán token → **Kết nối** → thêm tài khoản (số điện thoại → mã OTP → mật khẩu 2 lớp nếu có, hoặc quét QR).
Hướng dẫn đầy đủ có ảnh: [has-addons / telegram_calling](https://github.com/TriTue2011/has-addons/blob/main/telegram_calling/README.md).

## Cần biết

- Cổng **3333 chỉ để trong LAN**, đừng mở ra Internet. Thư mục dữ liệu chứa **phiên Telegram** — giữ kín.
- Không chạy cùng lúc với module web2api `telegram-user` trên cùng một tài khoản.

## Kho này gồm

| Thư mục | Là gì |
|---|---|
| `custom_components/telegram_call/` | Tích hợp Home Assistant (HACS: **một tích hợp mỗi kho**) |
| `telegram-gateway/` | Gateway Docker: FastAPI + TDLib + NTgCalls, REST + WebUI. Ảnh `ghcr.io/tritue2011/telegram-calling`. Chi tiết API: [`telegram-gateway/README.md`](telegram-gateway/README.md) |
| `tests/` | Test cấu trúc tích hợp (test gateway nằm trong `telegram-gateway/tests/`) |

Phát hành: đẩy thẻ `vX.Y.Z` khớp `version` trong `custom_components/telegram_call/manifest.json` **và** `telegram-gateway/VERSION`
→ Release (`telegram_call.zip`) + ảnh Docker `:X.Y.Z`. Độc lập hoàn toàn với kho Zalo-calling.
