# Báo cáo — Gói gateway Telegram chạy bằng Docker (khung P0)

Ngày: 2026-11-04. Phạm vi: CHỈ KHUNG + ĐÓNG GÓI, không đăng nhập, không
credential thật, không gửi tin/gọi tới ai, không đẩy image hay repo nào.
Mọi việc trong `~/workspace/telegram-docker/`.

## 1. Tài liệu đã đọc trước khi viết (theo thứ tự brief)

1. `~/workspace/telegram-thiet-ke-chi-tiet/thiet-ke.md` — thiết kế
   container gốc (REST, queue, audio, đóng gói). **Điểm sửa so với gốc:**
   engine A (Pyrogram) của thiết kế này đã bị T0 làm rớt cổng nên
   KHÔNG dùng; gateway viết theo engine B (xem dưới).
2. `~/workspace/telegram-t0b-kiem-chung/ket-qua.md` — cổng B qua:
   TDLib `sendCallSignalingData` + `updateNewCallSignalingData`,
   `callStateReady` đủ trường sang NTgCalls (kể cả
   `custom_parameters` — đúng trường A thiếu), tdjson 1.8.69 wheel
   cp312 cả amd64 + aarch64, client TDLib layer 232.
3. `~/workspace/telegram-w2a-module/thiet-ke.md` — mượn ngữ nghĩa REST,
   mã lỗi (409 ACCOUNT_BUSY/CALL_NOT_ACTIVE/ENDPOINT_BUSY, 429
   FLOOD_WAIT kèm `retry_after_seconds`, 502 MEDIA_ENGINE_ERROR, 503
   ACCOUNT_OFFLINE), giá trị mặc định (ring 45 s, attempts 2, interval
   15 s, max 180 s, queue 120 s/10), AudioHub PCM, envelope event.
4. `~/workspace/telegram-p0/giao/outbox/telegram-p0-bao-cao.md` — khung
   P0 module để khỏi lệch khái niệm (session TDLib trên đĩa, mức
   mock trung thực). **Không copy code module sang gateway** — gateway
   là tiến trình Python độc lập (FastAPI), chỉ mượn ngữ nghĩa.

## 2. Đã xây

Tiến trình Python asyncio duy nhất (FastAPI + web UI tĩnh), tại
`~/workspace/telegram-docker/`:

- `gateway/routes.py` — REST gốc `http://<server>:3333` (chỉ LAN):
  `health` (không auth), `accounts` (chỉ đọc, không bí mật),
  `send_message` (mock), `send_media`/`send_voice` (501 P4), `call`
  (trả ngay `dialing`, mock media), `answer_call`/`hangup_call`
  (state machine thật), `calls`, `history`/`contacts`/`groups`
  (501 P4), `events` poll fallback. Đường lạ 404, sai phương thức
  405, thân không phải dict 400, thân > 400 KB 400. Rate limit 120
  request/phút/token (health không tính).
- `gateway/store.py` — `/data`: `accounts.json` 0600 (không bí mật),
  `tdlib/<id>/khoa` 0600 (khóa mã hóa local TDLib, 32 byte hex),
  `tdlib/<id>/files/` 0700, `tai_khoan.json` 0600. `id` = 16 hex đầu
  SHA-256(phone). Bền qua restart (tạo lại store trên cùng thư mục
  → tài khoản + khóa còn nguyên).
- `gateway/login.py` — đăng nhập CHỈ qua web UI nội bộ (`/ui/api/*`),
  không qua API của HA: nhập số → `login_id` → nhập mã. Khung mock:
  mã đúng định dạng 5–6 chữ số được chấp nhận để tạo session khung;
  xác minh thật là P1. Chống flood thật: mã sai định dạng tính là
  thử sai, 3 lần sai → khóa login_id 15 phút; log chỉ đếm lần thử.
- `gateway/calls.py` — state machine 1-1/tài khoản
  (idle → dialing → ringing → active → ended); bận → 409
  ACCOUNT_BUSY hoặc vào hàng đợi FIFO (sâu 10, timeout 120 s, ưu tiên
  alarm > normal); đưa lên từ hàng đợi khi cuộc trước cúp (kèm claim
  endpoint). Thứ tự kiểm tra: tài khoản bận trước, endpoint bận sau.
- `gateway/audio.py` — AudioHub PCM: `MicSource`/`SpeakerSink`,
  `FileSource`/`FileSink` (kiểm thử), resample bằng ffmpeg, claim
  endpoint (`ENDPOINT_BUSY` khi mic/loa đã thuộc cuộc khác). Định
  dạng engine ghi rõ là tiền lệ group call (s16le 48 kHz), CHƯA xác
  minh cho gọi riêng tư (chờ T1).
- `gateway/engine.py` — vỏ phương án B, import lazy (gateway chạy
  mock không cần wheels): `TdlibSignaling` (setTdlibParameters từ
  thư mục files + file khoa; chưa đăng nhập → `EngineNotReady`),
  `NtgCallsMedia` (stub P2P), map glue `callStateReady` → NTgCalls
  trong docstring, caveat binding nhận bytes không nhận str.
- `gateway/events.py` — EventBridge: envelope `{id, type, account,
  timestamp, data}` (id đơn điệu tăng, khôi phục qua restart),
  webhook HA (`X-Gateway-Token`, retry 3 lần backoff rồi spool),
  spool `/data/events` + poll `GET /api/events?since=cursor`.
- `gateway/redact.py` — lọc log che token/OTP/2FA/api_id/api_hash/
  session; `mask_peer` che một phần peer trong log.
- `gateway/web/index.html` — web UI tĩnh tối thiểu + cảnh báo kiến
  trúc (một tài khoản, một nơi ở).
- Đóng gói: `Dockerfile` (python:3.12-slim + ffmpeg + pins),
  `docker-compose.yml` (bind mặc định `127.0.0.1:3333`, `./data:/data`
  riêng, restart unless-stopped, healthcheck `GET /health`, log
  rotation 10m × 3), `.env.example` (chỉ tên biến; GATEWAY_TOKEN
  sinh mới trên server bằng `openssl rand -hex 32`, api_id/api_hash
  do chủ máy tự tạo ở my.telegram.org), `VERSION` 0.1.0,
  `requirements.txt` (fastapi, uvicorn, httpx, tdjson==1.8.69,
  ntgcalls==3.0.6).

## 3. Test đã chạy + kết quả

Môi trường: venv riêng `~/workspace/telegram-docker/venv` (Python
3.12.3, pytest 9.2.0, tdjson 1.8.69, ntgcalls 3.0.6 — đúng pin T0/T0-B,
cài + import OK; ffmpeg 8.1.2 có sẵn). Không mạng, không tài khoản thật.

- **pytest 15/15 xanh** (`tests/test_gateway.py`, 0,76 s): health
  không auth; 401 + rate limit 429; accounts chỉ đọc (giá trị khóa
  session không lộ); send_message mock + 400/404; 501 kèm giai đoạn
  cho send_media/send_voice/history/contacts/groups; 404/405; thân
  không phải dict 400; luồng gọi (dialing → busy 409 → queue →
  answer → hangup → promote); ENDPOINT_BUSY khác tài khoản; login UI
  mock + khóa flood sau 3 mã sai; session bền qua restart + quyền
  0600/0700; cursor event đơn điệu tăng + poll; log redact;
  engine chưa sẵn sàng + quy tắc bytes; AudioHub claim/release.
- **Smoke test chạy thật:** `python -m gateway.main` với DATA_DIR
  tạm + token thử → `GET /health` 200 (`degraded`, 0 tài khoản,
  `mock_mode: true`); `/api/accounts` 200 rỗng; `/api/call` khi chưa
  có tài khoản → 503 ACCOUNT_OFFLINE đúng (không crash).
- **Compose kiểm cú pháp:** parse YAML OK (ports, volumes, restart,
  healthcheck đúng).
- **Chưa chạy:** build image Docker thật (máy này không còn Docker
  sau lần VM bị thay thế — build trên server hoặc máy có Docker);
  chạy container thật; import wheels trên ARM thật (T0/T0-B mới xác
  minh aarch64 bằng readelf).

## 4. Tiêu chí khung P0 — đạt/chưa đạt từng mục (trung thực)

| Tiêu chí (khung + mock, không tài khoản thật) | Kết quả |
|---|---|
| Tiến trình gateway độc lập chạy được | **ĐẠT Ở MỨC CODE + SMOKE:** boot + phục vụ REST thật trên máy này. **CHƯA ĐẠT Ở MỨC CONTAINER:** chưa build/chạy image Docker (máy này mất Docker). |
| REST đủ đường theo thiết kế | **ĐẠT Ở MỨC DISPATCH:** mọi đường trong bảng đều có, đúng mã lỗi; gửi/gọi thật là P1. |
| Session bền qua restart | **ĐẠT Ở MỨC MOCK:** tạo lại store trên cùng thư mục → tài khoản + khóa còn nguyên, quyền đúng. **Restart container thật chưa kiểm.** |
| Đăng nhập qua web UI (khung) | **ĐẠT Ở MỨC MOCK:** luồng login_id → mã → session khung + khóa flood. **Đăng nhập OTP/2FA/QR thật là P1.** |
| Event < 5 s (mock) | **ĐẠT Ở MỨC MOCK:** emit → poll trả ngay. **Event thật từ TDLib + webhook HA thật chưa có.** |
| Đóng gói Docker | **ĐẠT Ở MỨC FILE:** Dockerfile + compose + .env.example + README đầy đủ. **CHƯA ĐẠT Ở MỨC IMAGE:** chưa build/push (không đẩy image theo brief). |

## 5. Điểm thiết kế được điều chỉnh ở khung này (lý do cụ thể)

- **D-D1 — engine B thay A:** thiết kế container gốc dùng Pyrogram;
  T0 làm A rớt cổng (layer 158, thiếu `custom_parameters`) nên
  gateway dùng TDLib + NTgCalls (T0-B qua cổng). Session theo đó đổi
  từ file SQLite Pyrogram sang thư mục files + khóa mã hóa TDLib.
- **D-D2 — login tách khỏi `/api/*`:** thiết kế gốc đặt endpoint
  login dưới `/api/accounts`; khung này đặt dưới `/ui/api/*` để bề
  mặt HA (`/api/*`) không có đường đăng nhập nào — đúng luật "login
  chỉ qua web UI nội bộ" một cách cấu trúc, không chỉ quy ước.
- **D-D3 — thứ tự lỗi gọi:** tài khoản bận (ACCOUNT_BUSY) kiểm trước
  endpoint bận (ENDPOINT_BUSY), vì bận tài khoản là lỗi chính khi
  cùng tài khoản gọi chồng; endpoint bận chỉ có nghĩa khi khác tài
  khoản tranh cùng mic/loa.
- **D-D4 — app nghe 0.0.0.0 trong container:** Docker forward về IP
  của container nên app trong container phải nghe 0.0.0.0; LAN-only
  thực thi bằng bind phía host trong compose (`127.0.0.1:3333`),
  không phải bằng bind của app. Ghi rõ trong `main.py` + README để
  không ai "sửa" thành 127.0.0.1 trong container rồi mất kết nối.
- **D-D5 — rate limit thực thi được ở gateway:** khác module w2a
  (lõi không truyền danh tính khóa nên P0 không đặt giới hạn giả),
  gateway tự sở hữu auth nên giới hạn 120 request/phút/token được
  thực thi thật trong middleware của route.

## 6. Cảnh báo kiến trúc (ghi trong README + web UI, không giấu)

Một tài khoản MTProto KHÔNG chạy cùng lúc cả module w2a
`telegram-user` lẫn gateway Docker này (tranh session/updates). Lúc
triển khai: dùng 2 tài khoản phụ riêng (mỗi đường một tài khoản),
hoặc chỉ chạy một đường tại một thời điểm.

## 7. Còn chờ tài khoản thật / máy chủ thật để kiểm

1. Build + chạy image trên server có Docker; đăng nhập tài khoản phụ
   thật qua web UI (OTP/2FA/QR qua TDLib) + session bền qua 3 lần
   restart container (P1).
2. Gửi tin/event thật; import wheels trên ARM thật.
3. Glue TDLib→NTgCalls chạy thật + định dạng PCM gọi riêng tư (T1,
   sau P0/P1) — cuộc gọi thật tài khoản phụ ↔ tài khoản test.
4. P2 đàm thoại 2 chiều + cầu audio HA (định nghĩa transport
   HA↔gateway tương đương dạng vé luồng/khối JSON của đường module);
   P3 nhận; P4 phủ Zalo còn lại; P5 alarm policy + hardening.
5. Image đa kiến trúc + registry theo mẫu zalobot (build từng kiến
   trúc theo digest rồi gộp manifest `latest` + `{version}`) khi sẵn
   sàng phân phối.

## 8. Ranh giới đã giữ

Không đăng nhập tài khoản Telegram nào; không api_id/api_hash/token/
OTP thật (mọi chuỗi trong test đều GIẢ rõ ràng: token "test-token",
số điện thoại giả); không gửi tin, không gọi tới bất kỳ ai; không
đẩy image, không đẩy repo nào; không sửa module `telegram-user` hay
bất kỳ code nào khác. Không có bí mật nào trong code, test, compose,
`.env.example`, README hay báo cáo này.
