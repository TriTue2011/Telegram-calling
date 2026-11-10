# ✈️ Telegram Calling

Cổng **Telegram** cho Home Assistant: đăng nhập **tài khoản Telegram phụ**, nhắn tin, gọi thoại/video, đẩy sự kiện về HA.
Gồm **gateway** (Docker, TDLib + NTgCalls) và **tích hợp HA** `telegram_call` (HACS).

> ⚠️ **Dùng tài khoản phụ**, không dùng tài khoản chính. Một tài khoản chỉ đăng nhập ở **một nơi**.
> ⚠️ **Bản xem trước (0.x):** gateway và tích hợp đều có test; **cuộc gọi Telegram thật với loa R1/camera chưa kiểm tới nơi tới chốn** — lỗi thì gửi Nhật ký ở Issues.
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

> **Token gateway là gì?** Mật khẩu chung giữa gateway và tích hợp HA, không liên quan Telegram. Add-on: bỏ trống `gateway_token` thì tự sinh, in ra tab **Nhật ký** và giữ lại.
> Docker: bạn tự đặt trong `.env` (`openssl rand -hex 32`). Dán đúng chuỗi đó vào ô **Token gateway** khi *Thêm tích hợp*, và vào ô token của WebUI `/ui/`.

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
    init: true                # dọn ffmpeg mồ côi, không để lại tiến trình ma
    mem_limit: 512m           # đo: ~70 MB lúc rảnh, mỗi ffmpeg video ≤ ~160 MB; tăng nếu camera 4K
    memswap_limit: 512m       # không tràn sang swap
    cpus: "1.5"
    pids_limit: 256
    logging:
      driver: json-file
      options: {max-size: "10m", max-file: "3"}
    ports:
      - "127.0.0.1:3333:3333"   # HA ở máy khác: "IP_LAN:3333:3333" + firewall chỉ cho IP của HA
    volumes:
      - ./data:/data            # phiên Telegram nằm ở đây, giữ kín
    environment:
      GATEWAY_TOKEN: ${GATEWAY_TOKEN}
      TELEGRAM_API_ID: ${TELEGRAM_API_ID}
      TELEGRAM_API_HASH: ${TELEGRAM_API_HASH}
      GATEWAY_MOCK: "0"         # 0 = chạy thật; 1 = giả lập
      # VIDEO_PROFILE: "low"    # máy yếu: hình 480x270, 10 khung/giây (mặc định normal = 640x360, 15)
```

```
# .env (chmod 600). GATEWAY_TOKEN sinh bằng: openssl rand -hex 32
GATEWAY_TOKEN=
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
```

`docker compose up -d` → mở `http://<máy>:3333/ui/`, dán token → **Kết nối** → thêm tài khoản (số điện thoại → mã OTP → mật khẩu 2 lớp nếu có, hoặc quét QR).
Hướng dẫn đầy đủ có ảnh: [has-addons / telegram_calling](https://github.com/TriTue2011/has-addons/blob/main/telegram_calling/README.md).

## Dùng

Trên thiết bị **Telegram Call** (cùng kiểu thiết bị của Zalo Call): **Mic**, **Camera**, **Loa mặc định**, **Âm lượng cuộc gọi** (0 = giữ nguyên âm lượng loa;
đặt lúc nghe máy, trả lại khi kết thúc). Tiếng bên kia ra **loa R1 hoặc loa camera Imou/Dahua** (qua cửa bộ đàm của tích hợp tương ứng);
loa khác chưa hỗ trợ. Mic lấy từ camera, loa R1 hoặc địa chỉ luồng bất kỳ.

```yaml
service: telegram_call.make_call
data: {peer: "@ten_dang_nhap", video: true, volume: 70}   # gọi bằng tên đăng nhập Telegram
# peer cũng nhận "+84912345678" (số điện thoại, có mã nước) hoặc ID số (user_id: 123456789)
```

**Gọi cho một tài khoản Telegram khác:** dùng `peer` là **@tên đăng nhập** của họ (Telegram → Cài đặt → Tên người dùng). Gọi bằng **số điện thoại** (`+84…`)
chỉ được khi họ cho phép tìm bằng số; không tìm thấy thì gateway báo lỗi 404. Người nhận phải để Telegram cho phép **cuộc gọi từ bạn**
(Cài đặt → Quyền riêng tư → Cuộc gọi) và **không** là chính tài khoản phụ đang đăng nhập. Kênh/nhóm không gọi được.

### Nhắn tin (giống bộ dịch vụ của Zalo Call)

Người nhận: `chat_id` (số; âm = nhóm/kênh) **hoặc** `peer` (`@username`, `+số điện thoại`, ID số). Mọi dịch vụ có ô chọn trong HA.

| Dịch vụ | Làm gì |
|---|---|
| `send_message` | Gửi chữ; `format`: `plain` / `markdown` (MarkdownV2 của Telegram) / `html`; `reply_to` trả lời một tin |
| `send_photo` · `send_video` · `send_audio` · `send_file` | Gửi ảnh / video / file âm thanh / tệp từ đường dẫn HA, URL hoặc `media-source://`, kèm `caption` |
| `send_voice` | Tin thoại: từ tệp (mp3, m4a, wav… **tự đổi sang ogg/opus**) hoặc để gateway **đọc chữ thành giọng** (TTS) |
| `edit_message` · `delete_message` · `forward_message` · `add_reaction` · `mark_read` | Sửa, xoá, chuyển tiếp, thả/gỡ emoji, đánh dấu đã đọc |
| `get_history` · `get_chats` · `find_chat` · `get_contacts` | Đọc lịch sử, danh sách hội thoại, tìm theo tên, danh bạ (trả về cho automation bằng `response_variable`) |
| `translate_text` | Dịch chữ bằng bộ dịch của Telegram (`to_language`: vi, en, ja…) |
| `download_file` | Lưu tệp đính kèm của tin nhận được vào thư mục HA cho phép |

```yaml
service: telegram_call.send_message
data: {peer: "@ten_dang_nhap", message: "*Cảnh báo:* cửa mở", format: markdown}
```

**Nhận tin:** sự kiện `telegram_call_message` (tin đến: `chat_id`, `message_id`, `sender`, `kind`, `text`, `file_id`…) và cảm biến **Tin nhắn gần nhất**;
`telegram_call_send_failed` khi Telegram từ chối tin bạn gửi. Tin do chính bạn gửi không bắn sự kiện. Gọi: `make_call`, `answer_call`, `hangup_call`;
sự kiện `telegram_call_call_state` dùng **cùng tên trạng thái** với `zalo_call_call_state` (`incoming`, `ringing`, `answered`, `ended`, `not_answered`, `missed`, `error`).
Không biết ID? `get_contacts` / `find_chat`, hoặc trang `http://<máy>:3333/ui/`.

> Chưa có: nhận dạng giọng nói (Telegram chỉ cho tài khoản Premium) và gọi nhóm.

## Máy yếu (Raspberry Pi, NAS nhỏ)

- **Docker:** khối compose ở trên đã có trần RAM (512 MB), CPU (1,5 lõi), số tiến trình và xoay vòng nhật ký, để một cuộc gọi lỗi không làm cả máy đầy RAM/đĩa. Máy 1 GB RAM thì hạ `mem_limit` xuống `384m`.
- **Giảm hình:** `VIDEO_PROFILE=low` (480x270, 10 khung/giây thay cho 640x360, 15); add-on có tuỳ chọn `video_profile` tương ứng.
- **Camera: chọn luồng phụ (sub), không phải luồng chính.** Đo thật cùng một camera, ffmpeg giải mã + thu nhỏ: luồng chính ≈ **40%** một lõi, luồng phụ ≈ **16%**. Với go2rtc, dùng tên luồng phụ (ví dụ `phong-khach-sub`).

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
