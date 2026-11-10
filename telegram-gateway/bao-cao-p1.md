# Báo cáo P1+P2 — Gateway Telegram Docker (không push)

Ngày: 2026-11-05. Phạm vi: CHỈ viết code + test offline/mock trong
`~/workspace/telegram-docker/`. KHÔNG đẩy GitHub, KHÔNG chạm repo nào
khác, KHÔNG dùng tài khoản/credential thật, KHÔNG chạm thư mục
`telegram-p1` hay thư mục giao-repo nào. Không có api_id/api_hash/
token/OTP/mật khẩu thật nào trong code, test, README hay báo cáo này
(mọi chuỗi trong test đều GIẢ rõ ràng).

## 1. P1+P2 gồm gì

**P1 — đăng nhập + gửi thật qua TDLib (chỉ qua web UI nội bộ):**
- `gateway/tdclient.py` (mới): client TDLib thật qua tdjson 1.8.69 —
  setTdlibParameters (files dir + khóa file `khoa`) →
  checkDatabaseEncryptionKey → số điện thoại → OTP → 2FA (nếu server
  yêu cầu) → QR tùy chọn; gửi tin/media/voice qua MTProto; lỗi
  FLOOD_WAIT parse số giây thẳng từ message Telegram
  (`FLOOD_WAIT_<n>`); request JSON-encode sang bytes, trường bytes
  trong JSON là base64 (bằng chứng probe T0-B).
- `gateway/login.py`: luồng login_id → OTP → 2FA → QR, session bền
  (`store.ensure_session_dir` trước khi mở client; tài khoản thật
  status `online`, display_name từ getMe). Chống flood: 3 mã sai
  (sai định dạng HOẶC server từ chối) → khóa login_id 15 phút; log
  chỉ đếm lần thử, không ghi mã/OTP/2FA. Không có endpoint đăng nhập
  nào dưới `/api/*` (test quét route khẳng định).
- `gateway/routes.py`: `/api/send_message` thật khi tài khoản đã gắn
  client (không thì mock khi mock_mode, 503 khi engine thật mà
  offline); `/api/send_media`, `/api/send_voice` thật khi có client,
  ngược lại giữ 501 P4. Engine thật chỉ bật ở `main.py` khi
  `GATEWAY_MOCK=0` và có `TELEGRAM_API_ID`/`TELEGRAM_API_HASH`.

**P2 — gọi đi + audio:**
- `gateway/engine.py`: glue THẬT — `map_ready(callStateReady)` thuần
  (servers → RTCServer, protocol → versions/p2p_allowed,
  encryption_key → skip_exchange, custom_parameters passthrough,
  emojis) + `GlueSession` nối signaling 2 chiều (TDLib
  updateNewCallSignalingData ↔ NTgCalls send_signaling_data /
  on_signaling_data; `handle_update` định tuyến updateCall Ready).
  `TdlibSignaling`/`NtgCallsMedia` giờ ủy thác sang client/engine đã
  gắn, chưa gắn vẫn raise EngineNotReady như P0.
- `gateway/calls.py`: thêm trạng thái `ringing` (`mark_ringing`),
  `tts_audio_id` trên cuộc gọi; giữ nguyên dialing → active → ended,
  hàng đợi FIFO sâu 10 / timeout 120 s / ưu tiên alarm > normal,
  409 ACCOUNT_BUSY rồi mới ENDPOINT_BUSY.
- `gateway/audio.py`: `FrameQueue` (khối 200 ms mặc định ở 48 kHz mono
  s16le = 19.200 byte; mất khối = im lặng + đếm; hàng đợi đầy 50 khối
  → backpressure; khối quá cũ > ~2 s → bỏ + đếm) + AudioHub
  `push_in`/`pop_in`/`queue_playback`; `PipeSource`/`PipeSink` đầy đủ;
  `R1StreamSource/Sink`, `UsbMicSource/Sink` chỉ interface + tài liệu
  (start raise rõ ràng, cần phần cứng). `POST /api/audio/in`
  (mic HA theo khối, sai tốc độ thì resample ffmpeg, đầy → 429
  BACKPRESSURE) và `POST /api/audio/upload` (nạp TTS HA theo khối
  ≤ 300 KB → `tts_audio_id`, xóa sau cuộc gọi hoặc sau 1 giờ).
- `gateway/tts.py` (mới): interface `Synthesizer` inject được;
  `EdgeTtsSynthesizer` mặc định (vi-VN-HoaiMyNeural, +0%) qua package
  edge_tts + ffmpeg decode ra PCM; `TtsUploadStore` ráp file + prune.
  `/api/call` mode tts dùng `tts_text` (tổng hợp) hoặc `tts_audio_id`
  (file HA); lỗi synthesizer → 502 MEDIA_ENGINE_ERROR và cuộc bị cúp.

**Defaults giữ nguyên:** ring 45 s, attempts 2, interval 15 s,
max 180 s, queue 120 s/10, rate limit 120 req/phút/token, mã lỗi đủ bộ
(409 ACCOUNT_BUSY/CALL_NOT_ACTIVE/ENDPOINT_BUSY, 429 FLOOD_WAIT kèm
retry_after_seconds từ Telegram / LOGIN_LOCKED / BACKPRESSURE /
RATE_LIMITED, 502 MEDIA_ENGINE_ERROR, 503 ACCOUNT_OFFLINE).

## 2. File đổi

- Mới: `gateway/tdclient.py`, `gateway/tts.py`,
  `bao-cao-p1.md` (file này).
- Sửa: `gateway/engine.py`, `gateway/login.py`, `gateway/store.py`
  (ensure_session_dir, status online), `gateway/calls.py`,
  `gateway/audio.py`, `gateway/routes.py`, `gateway/config.py`
  (TELEGRAM_API_ID/HASH), `gateway/main.py` (nối factory + Edge TTS
  khi không mock), `gateway/web/index.html`, `README.md`,
  `tests/test_gateway.py` (thêm 19 test P1+P2).
- KHÔNG ĐỔI: `Dockerfile`, `docker-compose.yml`, `.env.example`,
  `.github/workflows/build-test.yml`, `requirements.txt`, `VERSION`
  (P1+P2 không bắt buộc sửa chúng — xem điểm lệch D-P2 dưới).

## 3. Test: 34/34 xanh

Chạy `PYTHONPATH=. venv/bin/pytest tests/test_gateway.py`
(venv sẵn tdjson 1.8.69 + ntgcalls 3.0.6): **34/34 xanh** — 15 test cũ
giữ nguyên xanh + 19 test P1+P2 mới. Bao phủ mới: login thật qua fake
tdjson (OTP ok/sai/khóa flood/2FA/QR), FLOOD_WAIT 60 s lấy từ server,
gửi tin/media/voice thật qua fake, không endpoint login dưới /api/*,
session thật bền qua restart + quyền 0600/0700, gọi busy → queue →
promote ưu tiên alarm + hết hạn queue_timeout, glue mapping với
fixture callStateReady, signaling 2 chiều qua GlueSession, TTS vào cuộc
gọi + upload HA + xóa sau hangup, synthesizer lỗi → 502, PCM framing
qua AudioHub (mất khối = im lặng + đếm, backpressure 429 ở cả tầng
FrameQueue lẫn route audio/in), Pipe/File roundtrip, webhook retry 3
lần rồi spool + cursor đơn điệu qua restart, R1/USB chỉ interface,
dispatch mã lỗi đủ bộ.

Boot smoke ngoài Docker (không cần Docker, Docker trên máy này không
chạy được container nên không cố docker build):
`GATEWAY_TOKEN=... DATA_DIR=/tmp/gw-smoke python -m gateway.main` →
`GET /health` 200 (`degraded`, 0 tài khoản, `mock_mode: true`);
`POST /api/call` chưa có tài khoản → 503 ACCOUNT_OFFLINE;
`GET /api/accounts` 200 rỗng.

## 4. Điểm lệch so với thiết kế (nếu có)

- **D-P1 — đường mock giữ lại khi chưa bật engine thật:** khi
  `GATEWAY_MOCK=1` (mặc định) hoặc chưa có factory, login/gửi giữ hành
  vi khung P0 và route trả `mock: true` rõ ràng, thay vì chặn cứng.
  Lý do: giữ 15 test cũ xanh và giữ gateway boot được khi chưa cấu
  hình TDLib; đường thật là đường duy nhất khi `GATEWAY_MOCK=0` +
  đủ api_id/api_hash. Đây là lệch có chủ ý, ghi rõ trong README.
- **D-P2 — Edge TTS cần package chưa có trong requirements:**
  `EdgeTtsSynthesizer` import `edge_tts` lazy; venv hiện tại KHÔNG có
  edge-tts nên TTS mặc định trên image hiện tại sẽ raise RuntimeError
  rõ ràng → route trả 502, không giả vờ chạy. `requirements.txt`
  KHÔNG thêm (giữ nguyên đóng gói theo brief); khi build image server
  phải thêm `edge-tts` (ghi trong README «Còn chờ»). Test dùng fake
  synthesizer nên không cần mạng/package.
- **D-P3 — send_media/send_voice thật gửi dạng document/voice-note
  chung:** `send_media` hiện gửi `inputMessageDocument` cho mọi file
  (chưa phân loại ảnh/video theo mime), `send_voice` gửi
  `inputMessageVoiceNote`. Tinh chỉnh theo mime là việc P4, không đổi
  API đã định.
- **D-P4 — RTCServer constructor:** `RealNtgCallsMedia` thử kwargs
  trước, fallback thứ tự vị trí theo tài liệu pytgcalls nếu binding
  không nhận kwargs. Chữ ký vị trí chính xác chỉ T1 mới chốt được.
- **D-P5 — media NTgCalls thật chưa nối vào route /api/call:**
  route gọi đi hiện chạy state machine + TTS playback buffer; việc
  khởi động NTgCalls engine thật với peer thật (async runtime +
  `create_p2p_call`/`connect_p2p` trong ngữ cảnh async) thuộc T1 —
  glue mapping/signaling đã có và đã test với fake, nhưng chưa có
  đường chạy engine thật end-to-end trong route. Không claim ngược lại.

## 5. Chưa làm (cần bước sau)

1. **T1 — gọi thật bằng tài khoản phụ** ↔ tài khoản test: chốt glue
   chạy thật, định dạng PCM gọi riêng tư (TODO T1 trong code:
   `ENGINE_PCM_FORMAT` s16le 48 kHz mono ở `audio.py` là tiền lệ
   group call, CHƯA xác minh cho gọi riêng tư — không claim đã đúng),
   server chấp nhận layer 232 + layer gọi NTgCalls, RTCServer ctor.
2. **Build image Docker thật + chạy trên server** (máy này không chạy
   được container; Dockerfile/compose/workflow giữ nguyên, chưa build).
3. **P3 nhận cuộc gọi** (incoming: updateCall → ring → answer/hangup),
   **P4** (history/contacts/groups, media theo mime, voice-note riêng),
   **P5** (alarm policy đầy đủ + hardening).
4. Thêm `edge-tts` vào requirements khi build image server (xem D-P2).
5. Đăng nhập tài khoản phụ thật qua web UI + session bền qua 3 lần
   restart container (code P1 đã có, chưa chạy với tài khoản thật).

## 6. Cảnh báo kiến trúc (giữ nguyên, không đổi)

Một tài khoản MTProto KHÔNG chạy cùng lúc cả module web2api
`telegram-user` lẫn gateway Docker này (tranh session/updates). Lúc
triển khai: dùng 2 tài khoản phụ riêng (mỗi đường một tài khoản),
hoặc chỉ chạy một đường tại một thời điểm.
