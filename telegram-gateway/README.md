# Telegram Gateway (Docker) — P1 + P2

Gateway Telegram độc lập chạy bằng Docker, song song với module web2api
`telegram-user` (P0 đã xong). Gói này đã qua P1 (đăng nhập + gửi thật
qua TDLib) và P2 (gọi đi: state machine + glue + TTS + AudioHub PCM).
Chưa có cuộc gọi thật với tài khoản phụ (T1) — xem «Còn chờ» cuối file.

## ⚠️ Cảnh báo kiến trúc — đọc trước khi triển khai

**Một tài khoản MTProto KHÔNG chạy cùng lúc cả module web2api
`telegram-user` lẫn gateway Docker này.** Hai nơi ở cùng giữ một session
sẽ tranh session/updates: TDLib và MTProto đều giữ kết nối thường trực,
một bên reconnect sẽ làm bên kia mất update, và server có thể coi là
phiên bất thường. Lúc triển khai, chọn một trong hai:

1. **Dùng 2 tài khoản phụ riêng** — mỗi đường một tài khoản (khuyến nghị
   nếu muốn chạy song song), hoặc
2. **Chỉ chạy một đường tại một thời điểm** — dừng hẳn đường kia trước
   khi khởi động đường này.

Đây là lý do kỹ thuật, không phải quy tắc tạm thời — vi phạm sẽ gây mất
tin nhắn/cuộc gọi đến một cách khó chẩn đoán.

## Engine: phương án B (TDLib + NTgCalls)

Thiết kế container gốc (2026-11-04) dùng phương án A (Pyrogram), nhưng
T0 đã làm A rớt cổng (Pyrogram 2.0.106 khai layer 158, thiếu
`custom_parameters`) và T0-B đã cho B qua cổng. Gateway này viết theo B:

- **Báo hiệu:** TDLib qua `tdjson==1.8.69` — `createCall` / `acceptCall` /
  `discardCall` / `updateCall` / `sendCallSignalingData` /
  `updateNewCallSignalingData` (API signaling tồn tại từ TDLib 1.7.0).
- **Media:** NTgCalls `ntgcalls==3.0.6` — entry point P2P + PCM vào/ra đã
  xác minh ở T0 (định dạng PCM gọi riêng tư vẫn chờ T1; tiền lệ group
  call là s16le 48 kHz, chưa đủ bằng chứng cho gọi riêng tư).
- **Glue THẬT (`gateway/engine.py`: `map_ready` + `GlueSession`):**
  `callStateReady` → `servers` → `connect_p2p`, `protocol` → versions/
  `p2p_allowed`, `encryption_key` → `skip_exchange`/`exchange_keys`,
  `custom_parameters` → `connect_p2p(custom_parameters)`,
  `emojis` → `get_emojis_fingerprint`, signaling hai chiều qua
  `sendCallSignalingData` ↔ `send_signaling_data`/`on_signaling_data`.
  Caveat binding: tdjson nhận **bytes**, không nhận str — request
  JSON-encode sang bytes, trường bytes trong JSON là base64 (bằng
  chứng probe T0-B). Glue đã test với fake hai đầu; chạy thật với
  peer thật thuộc T1.
- **Session TDLib (khác Pyrogram):** mỗi tài khoản = thư mục files +
  khóa mã hóa local (file `khoa` 0600, `files/` 0700), không phải file
  SQLite của Pyrogram. Mất khóa = phải đăng nhập lại.

## Chạy trên server

```bash
cp .env.example .env
# Điền GATEWAY_TOKEN: sinh bằng openssl rand -hex 32, chmod 0600 .env
docker compose up -d --build
curl http://127.0.0.1:3333/health
```

Yêu cầu: Docker + compose, `tdjson==1.8.69`, `ntgcalls==3.0.6`,
glibc ≥ 2.28 (cho ntgcalls), `ffmpeg` (trong image sẵn).
Image đa kiến trúc (amd64 + arm64) theo mẫu zalobot khi đẩy lên
registry: build từng kiến trúc theo digest rồi gộp manifest
(`latest` + `{version}` từ file `VERSION`) — chưa làm ở khung này
(không đẩy image).

## REST — gốc `http://<server>:3333`, chỉ LAN

Auth: `Authorization: Bearer <GATEWAY_TOKEN>`. Chỉ `GET /health`
không cần auth. Rate limit: 120 request/phút/token.

| Đường | P1+P2 |
|---|---|
| `GET /health` | `{status, version, uptime_seconds, accounts_total, accounts_online, mock_mode}` |
| `GET /api/accounts` | Chỉ đọc, không bí mật |
| `POST /api/send_message` | Thật qua TDLib khi tài khoản đã gắn client; chưa gắn → mock (mock_mode) hoặc 503 khi engine thật mà offline |
| `POST /api/send_media`, `POST /api/send_voice` | Thật qua TDLib khi có client; chưa có client → 501, giai đoạn P4 (đường tinh chỉnh mime/voice-note riêng là P4) |
| `POST /api/call` | Trả ngay `{call_id, status: "dialing"}`; mode tts kèm `tts_text` (Edge TTS) hoặc `tts_audio_id` (file HA đã nạp) |
| `POST /api/answer_call`, `POST /api/hangup_call` | State machine thật (dialing → ringing → active → ended); hangup xóa file TTS của cuộc đó |
| `GET /api/calls` | Danh sách cuộc gọi |
| `GET /api/history`, `/api/contacts`, `/api/groups` | 501, giai đoạn P4 |
| `GET /api/events?since=<cursor>` | Poll fallback (cursor đơn điệu tăng, bền qua restart) |
| `POST /api/audio/in` | Mic HA → engine: khối JSON `{call_id, seq, pcm_b64, sample_rate, channels}`; mất khối = im lặng + đếm; đầy hàng đợi → 429 BACKPRESSURE |
| `POST /api/audio/upload` | Nạp audio TTS HA theo khối base64 (≤ 300 KB/khối) → `{tts_audio_id}`; xóa sau cuộc gọi hoặc sau 1 giờ |
| Đường lạ → 404, sai phương thức → 405, thân không phải dict → 400 |

Mã lỗi (ngữ nghĩa theo thiết kế module): 409 `ACCOUNT_BUSY`
(tài khoản đang trong một cuộc 1-1), `CALL_NOT_ACTIVE`,
`ENDPOINT_BUSY` (mic/loa đã thuộc cuộc khác); 429 `FLOOD_WAIT` kèm
`retry_after_seconds` lấy thẳng từ Telegram (gateway không tự bịa số
này), `LOGIN_LOCKED` (khóa đăng nhập), `BACKPRESSURE` (HA đẩy mic
nhanh hơn gateway tiêu thụ), `RATE_LIMITED` (quá 120 request/phút);
502 `MEDIA_ENGINE_ERROR`; 503 `ACCOUNT_OFFLINE`.

## Đăng nhập — chỉ qua web UI nội bộ

Mở `http://<server>:3333/ui/` (LAN-only). Đăng nhập **không** qua API
của HA integration: `/ui/api/accounts` → nhập số → `/ui/api/accounts/
{login_id}/code` → nhập mã OTP → nếu server yêu cầu 2FA thì
`/ui/api/accounts/{login_id}/password`; hoặc QR tùy chọn:
`/ui/api/accounts/qr` → quét → `/ui/api/accounts/{login_id}/qr-confirm`.
Session TDLib bền trên đĩa (thư mục files 0700 + file khoa 0600 +
`accounts.json` 0600 không chứa bí mật). Chống flood: 3 mã sai → khóa
login_id 15 phút; log chỉ đếm lần thử, không ghi mã. Engine thật bật
khi `GATEWAY_MOCK=0` và có `TELEGRAM_API_ID`/`TELEGRAM_API_HASH` (chủ
máy tự tạo ở my.telegram.org); mặc định mock thì login/gửi giữ hành vi
khung P0 và route trả `mock: true` để phân biệt rõ. Xóa tài khoản/
session chỉ qua web UI có xác nhận.

## Nối HA

- HA gọi REST trên kèm Bearer token (config flow kiểm `GET /health`
  rồi `GET /api/accounts`, timeout 10 s).
- Event về HA: chính = webhook POST tới `HA_WEBHOOK_URL` kèm header
  `X-Gateway-Token` (retry 3 lần backoff rồi spool); dự phòng = poll
  `GET /api/events?since=cursor` (dedup theo event id).
- Envelope event: `{id, type, account, timestamp, data}`; loại P0:
  `message_new`, `call_incoming`, `call_ended`, `account_status`.
- Audio HA (P2): mic từ HA lên bằng khối JSON `POST /api/audio/in`
  `{call_id, seq, pcm_b64, sample_rate, channels}` (khối 200 ms–1 s;
  mất khối = im lặng + đếm; hàng đợi đầy → 429 BACKPRESSURE; khối quá
  cũ > ~2 s bị bỏ). Loa về HA: bộ đệm phát TTS trong AudioHub +
  file TTS của HA qua `POST /api/audio/upload` (xóa sau cuộc gọi hoặc
  sau 1 giờ). Endpoint R1 dạng luồng và mic/loa USB ở P2 chỉ là
  interface + tài liệu (cần phần cứng/giao thức thật mới triển khai);
  FileSource/FileSink và PipeSource/PipeSink đã làm đầy đủ cho kiểm
  thử/endpoint tùy chỉnh. Định dạng PCM engine mặc định s16le 48 kHz
  mono — tiền lệ group call, CHƯA xác minh cho gọi riêng tư (TODO T1).

## Bảo mật

- REST chỉ LAN: compose bind mặc định `127.0.0.1:3333`. Nếu HA ở máy
  khác trong LAN thì bind IP LAN cụ thể + firewall chỉ cho IP HA.
  **KHÔNG** bind `0.0.0.0` không kèm firewall, **KHÔNG** port-forward
  ra Internet, **KHÔNG** đưa qua Cloudflare Tunnel hay reverse proxy
  công khai — REST này chỉ có Bearer token, không đủ cứng để phơi ra
  Internet.
- `.env` 0600, sinh mới trên server, không copy từ máy khác.
  `accounts.json` 0600 (không bí mật), session TDLib: `khoa` 0600,
  `files/` 0700, sổ tài khoản 0600. Không bí mật nào qua API hay vào
  log (bộ lọc redact token/OTP/2FA/api_id/api_hash/session).
- Tài khoản dùng cho gateway là tài khoản phụ, tuyệt đối không tài
  khoản chính. Flood-wait của Telegram được tôn trọng (không retry
  ngay).

## Bố cục

```
gateway/            tiến trình Python (FastAPI + managers + AudioHub)
  main.py           điểm vào (uvicorn); LAN-only ở tầng compose;
                    nối TdlibClient + EdgeTts khi GATEWAY_MOCK=0
  routes.py         REST + web UI backend (/ui/api/*) + audio/in, audio/upload
  config.py         cấu hình từ .env (GATEWAY_TOKEN bắt buộc;
                    TELEGRAM_API_ID/HASH cho engine thật)
  store.py          accounts.json + session TDLib trên đĩa
  tdclient.py       client TDLib thật (bytes, FLOOD_WAIT, OTP/2FA/QR, gửi)
  login.py          đăng nhập web UI (TDLib thật / khung mock) + chống flood
  calls.py          state machine 1-1 + hàng đợi FIFO ưu tiên + ringing
  audio.py          AudioHub PCM (MicSource/SpeakerSink, FrameQueue,
                    File/Pipe đầy đủ, R1/USB chỉ interface, ffmpeg resample)
  tts.py            Edge TTS (inject được) + kho nạp audio TTS của HA
  engine.py         TDLib + NTgCalls thật: map_ready + GlueSession 2 chiều
  events.py         EventBridge (webhook + spool + poll fallback)
  errors.py         mã lỗi theo thiết kế module
  redact.py         che bí mật khỏi log
  web/index.html    web UI tĩnh tối thiểu
tests/              pytest (mock/fake, không mạng, không tài khoản thật)
Dockerfile          python:3.12-slim + ffmpeg + pins (KHÔNG ĐỔI ở P1+P2)
docker-compose.yml  bind 127.0.0.1:3333, ./data:/data, healthcheck (KHÔNG ĐỔI)
.env.example        chỉ tên biến, không giá trị thật (KHÔNG ĐỔI)
VERSION             0.1.0
```

## Còn chờ (sau P1+P2)

1. T1 — cuộc gọi thật tài khoản phụ ↔ tài khoản test: chốt glue
   chạy thật, định dạng PCM gọi riêng tư, server chấp nhận layer 232
   + layer gọi NTgCalls. Đây là bước bắt buộc trước khi claim gọi
   thật chạy được.
2. Đăng nhập tài khoản phụ thật qua web UI (OTP/2FA/QR qua TDLib) +
   session bền qua 3 lần restart container (P1 đã có code, chưa chạy
   với tài khoản thật).
3. Gửi tin/event thật; import wheels trên ARM thật (T0/T0-B mới xác
   minh aarch64 bằng readelf, chưa chạy thử).
4. P3 nhận cuộc gọi; P4 phủ còn lại (history/contacts/groups,
   media theo mime, voice-note riêng); P5 alarm policy + hardening.
5. Image đa kiến trúc + registry (mẫu zalobot) khi sẵn sàng phân phối.
   Lưu ý: Edge TTS mặc định cần package `edge-tts` trên image — hiện
   `requirements.txt` chưa thêm (giữ nguyên đóng gói theo brief P1+P2);
   khi build image server phải thêm `edge-tts` hoặc TTS mặc định sẽ
   trả 502 rõ ràng thay vì chạy.
