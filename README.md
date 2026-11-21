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

Thiết bị **Telegram Call** có các ô: **Mic**, **Camera**, **Loa mặc định**, **Âm lượng cuộc gọi** (0 = giữ nguyên âm lượng loa; đặt lúc nghe máy, trả lại khi kết thúc),
cảm biến **Cuộc gọi** và **Tin nhắn gần nhất**. Tiếng bên kia ra **loa R1 hoặc loa camera Imou/Dahua** (qua cửa bộ đàm của tích hợp tương ứng); loa khác chưa hỗ trợ.
Mic lấy từ camera, loa R1 hoặc địa chỉ luồng bất kỳ. Cách gọi, nhận cuộc gọi, nhắn tin và ví dụ automation: mục dưới.

> Chưa có: nhận dạng giọng nói (Telegram chỉ cho tài khoản Premium) và gọi nhóm.

## Nhiều tài khoản

Gateway hỗ trợ nhiều tài khoản trong một tiến trình. Đăng nhập các tài khoản muốn dùng trên giao diện gateway,
rồi vào **Cài đặt → Thiết bị & dịch vụ → Thêm tích hợp → Telegram Call**. Nhập cùng địa chỉ và token gateway,
chọn tài khoản đang online. **Thêm một mục cấu hình cho từng tài khoản muốn dùng trong HA**; để thêm tài khoản thứ hai,
lặp lại thao tác Thêm tích hợp và chọn số khác. Tài khoản offline hoặc đang chờ mã đăng nhập không xuất hiện trong danh sách.
Gateway chưa có tài khoản online thì HA báo lỗi và yêu cầu đăng nhập trên gateway trước.

Mỗi mục có thiết bị riêng mang tên **Telegram Call — <tên hiển thị>**; khi chưa có tên thì hiện số che giữa.
Tên đổi trên Telegram được cập nhật trong HA ở lần hỏi hồ sơ tiếp theo (khoảng 60 giây).
Các `entity_id` đã lưu trong registry và `unique_id` được giữ nguyên, nên automation dùng thực thể cũ tiếp tục tìm đúng thực thể.
Danh bạ, thiết bị mặc định, quy tắc và cuộc gọi của mỗi mục thuộc riêng tài khoản đó; gửi bằng thực thể `notify` dùng tài khoản sở hữu thực thể.

**Tài khoản mặc định:** mục cũ/mục đầu tiên nạp thành công được đánh dấu mặc định và lưu lại qua khởi động lại.
Đổi tại **Telegram Call → Cấu hình → Tài khoản mặc định cho dịch vụ không chỉ định tài khoản**.
Mọi dịch vụ `telegram_call.*` chọn theo thứ tự **`config_entry_id` → `account` → mặc định**.
`account` nhận số điện thoại đầy đủ có mã nước hoặc tên hiển thị (không phân biệt hoa thường).
Tên không tồn tại hoặc trùng nhiều tài khoản báo lỗi; hãy dùng số điện thoại hoặc `config_entry_id` khi tên trùng.
Các automation/blueprint cũ không có hai trường này tiếp tục chạy trên mặc định khi thêm mục thứ hai.
Nếu mục mặc định tạm thời chưa nạp, dịch vụ dùng mục đầu tiên đang nạp; nếu chưa nạp mục nào thì báo lỗi.

Ví dụ gửi từ một tài khoản cụ thể (các số bên dưới là số giả):

```yaml
action: telegram_call.send_message
data:
  account: "+84900000001"                # hoặc tên hiển thị: "Cơ quan"
  peer: "@ten_nguoi_nhan"
  message: "Cửa đã mở"
```

Các sự kiện `telegram_call_call_state` (kể cả `video_request`), `telegram_call_message`,
`telegram_call_send_failed`, `telegram_call_alert_done` đều thêm `account` là số điện thoại đầy đủ.
Các khoá cũ vẫn giữ nguyên. Automation nghe sự kiện không lọc tài khoản sẽ nhận sự kiện của mọi mục đã nạp;
để chỉ xử lý một tài khoản và trả lời bằng đúng tài khoản nhận tin, dùng:

```yaml
automation:
  - alias: Telegram cơ quan - xác nhận tin nhắn
    triggers:
      - trigger: event
        event_type: telegram_call_message
        event_data:
          account: "+84900000001"
    actions:
      - action: telegram_call.send_message
        data:
          account: "{{ trigger.event.data.account }}"
          chat_id: "{{ trigger.event.data.chat_id }}"
          reply_to: "{{ trigger.event.data.message_id }}"
          message: "Đã nhận tin"
```

Tương tự, automation áp quy tắc cuộc gọi có thể truyền `account: "{{ trigger.event.data.account }}"`
cùng `call_id` để xử lý đúng tài khoản của sự kiện.

## Cách dùng các dịch vụ

Mở **Công cụ nhà phát triển → Hành động**, gõ `telegram_call.` để chọn dịch vụ. Người nhận là `chat_id` (số; âm = nhóm/kênh) **hoặc** `peer` (`@username`, `+số điện thoại`, ID số).
Không biết ID: `get_contacts` (lọc theo tên) hoặc `find_chat`, rồi lấy `id` trong kết quả.

### Gọi đi

```yaml
service: telegram_call.make_call
data:
  peer: "@ten_dang_nhap"               # hoặc user_id: 123456789, hoặc "+84912345678"
  video: true                          # false = thoại
  volume: 70                           # tuỳ chọn, 0-100: âm lượng loa lúc gọi (hết cuộc tự trả lại)
  speaker: media_player.loa_r1         # tuỳ chọn; bỏ trống = ô «Loa mặc định» trên thiết bị Telegram Call (loa R1 / camera Imou-Dahua)
  mic: camera.phong_khach              # tuỳ chọn: tiếng gửi sang người nhận (camera, loa R1) hoặc mic_url: rtsp://...
  camera: camera.phong_khach           # tuỳ chọn (video): hình gửi đi
```

Người nhận phải cho phép cuộc gọi từ bạn (Telegram → Quyền riêng tư → Cuộc gọi). Kết thúc: `telegram_call.hangup_call`.

**Thoại và video: chỉ gọi 1-1.** Cuộc gọi video 1-1 chạy cả gọi đi (`video: true`) lẫn nhận (hình gửi đi là camera đã chọn; không chọn thì hình đen). **Cuộc gọi nhóm KHÔNG hỗ trợ** (kể cả nhóm có người trong đó): quy tắc không có phạm vi «nhóm» như Zalo Call. **Xin chuyển sang video giữa cuộc thoại:** người gọi bấm «Bắt đầu video» → sự kiện `telegram_call_call_state` với `state: video_request`. Quy tắc có trường `video_request`: `ask` (mặc định, riêng tư: chỉ báo sự kiện), `accept` (đồng ý, gửi hình đen), `accept_camera` (đồng ý, gửi camera đã chọn). Đồng ý bằng tay: dịch vụ `accept_video` hoặc nút **«Chuyển sang video»** trên thiết bị. (Cơ chế dựa trên NTgCalls, kiểm bằng test giả lập; chưa kiểm bằng cuộc gọi thật.)

### Nhận cuộc gọi

Có người gọi tài khoản Telegram → sự kiện **`telegram_call_call_state`** (và cảm biến *Cuộc gọi*). `state`:
`incoming` · `ringing` · `connecting` · `answered` · `ended` · `not_answered` · `missed` · `error`; kèm `call_id`, `user_id`, `outgoing`, `video`, `seconds`, `error`
(cùng tên trạng thái với `zalo_call_call_state`, nên một automation dùng được cho cả hai). Nghe máy: `telegram_call.answer_call` (bỏ `call_id` nếu chỉ có một cuộc đang đổ chuông).

```yaml
automation:
  - alias: Telegram gọi đến → báo điện thoại, tự nghe sau 10 giây
    triggers:
      - trigger: event
        event_type: telegram_call_call_state
        event_data: {state: incoming, outgoing: false}
    actions:
      - action: notify.mobile_app_dien_thoai
        data: {message: "Telegram gọi đến (ID {{ trigger.event.data.user_id }})"}
      - delay: 10
      - action: telegram_call.answer_call
```

**Kịp thời và đúng trạng thái.** Tích hợp hỏi gateway mỗi 1,5 giây khi rảnh (1 giây khi có cuộc gọi) nên cuộc gọi đến kích hoạt automation trong khoảng 1,5 giây. Sự kiện luôn bắn **sau** khi cảm biến *Cuộc gọi* đã đổi (automation đọc cảm biến thấy trạng thái mới). Cảm biến là `idle` khi không có cuộc gọi nào đang diễn ra; kết quả của cuộc vừa xong (`ended`, `missed`, `not_answered`…) nằm ở thuộc tính `last_state`.

**Ô chọn sẵn «nối ngay» / «khi nói trực tiếp».** `make_call` và `answer_call` nhận thêm `connect_now` (tích mic / loa / camera: thứ nào nối NGAY khi nghe máy; không tích thì mic im lặng, camera gửi hình đen, loa không nối; tích camera = cuộc gọi VIDEO) và `connect` (thứ nối khi người nhà bấm «Người nhà nói trực tiếp» / `take_over_call`) — cùng ý nghĩa với quy tắc `apply_call_rule` và với `zalo_call`. Không điền `connect_now` thì giữ như trước: mọi thiết bị đã chọn hoặc mặc định nối ngay.

**Thiết bị lấy từ đâu (cùng thứ tự với `zalo_call`).** (1) ô khai ngay trong lời gọi dịch vụ (`mic`, `mic_url`, `camera`, `speaker`); (2) quy tắc của automation cho cuộc gọi ĐẾN (`apply_call_rule`); (3) thiết bị mặc định trên thiết bị «Telegram Call»; (4) không có gì: mic im lặng, hình đen, không loa. Gọi đi bằng `make_call` mà không khai và cũng chưa đặt mặc định thì dịch vụ **báo lỗi nêu đúng phần thiếu** (mic hoặc loa; thêm camera nếu gọi video) thay vì gọi một cuộc im lặng; muốn gọi im lặng thì đặt `connect_now: []`. `answer_call` không chặn (chặn là mất cuộc gọi), cuộc không có thiết bị thì chỉ nghe máy im lặng.

**`make_call` có lời nhắn** (giống `make_call` của `zalo_call`): điền `message` thì khi người nhận nghe máy, giọng đọc đọc lời đó `repeat` lần (1-3; chọn giọng bằng `pipeline` / `tts_engine` / `language`), rồi cúp nếu bật `hangup_after`, không thì nối thiết bị nhà đã chọn để nói chuyện. Cuộc gọi cảnh báo thuần vẫn dùng `alert_call` (nhận cùng các ô, thêm `after`).

**Nghe máy bằng thiết bị riêng.** `answer_call` nhận `speaker`, `mic` (hoặc `mic_url`), `camera`, `volume` như `make_call`: đặt cho cuộc này trước khi nghe, hết cuộc tự trả về thiết bị mặc định.

#### Quy tắc cuộc gọi (giống Zalo Call)

Cùng mô hình với `zalo_call`: **automation là chính, mỗi cuộc gọi đến kích hoạt automation** và automation gọi `telegram_call.apply_call_rule` cho đúng cuộc gọi đó. Quy tắc chỉ sống đúng một cuộc gọi (tắt hay xoá automation là hết quy tắc); nhiều automation cho cùng một người vẫn chạy đủ: quy tắc **cụ thể hơn thắng** (người > thẻ > danh bạ > người lạ), cùng bậc thì mỗi quy tắc báo ra loa của riêng nó, còn nghe máy / thiết bị / lời nhắn theo quy tắc đầu tiên bật `answer`. **Không có quy tắc = chỉ đổ chuông.**

```yaml
automation:
  - alias: Telegram - nghe máy cuộc gọi của vợ
    triggers:
      - trigger: event
        event_type: telegram_call_call_state
        event_data: {state: incoming}
    conditions: "{{ trigger.event.data.user_id == 123456789 }}"
    actions:
      - action: telegram_call.apply_call_rule
        data:
          call_id: "{{ trigger.event.data.call_id }}"
          scope: thread                          # thread (người) · tag (thẻ) · contacts (danh bạ) · strangers (người lạ)
          source: "{{ this.entity_id }}"         # chống áp lặp, ghi rõ ai thắng trong nhật ký
          answer: true
          answer_delay: 5
          connect_now: [mic, speaker]            # nối NGAY khi nghe máy; không tích = mic im lặng (video: hình đen)
          mic: assist_satellite.r1
          speaker: media_player.r1_loa
          volume: 40
          announce: true                         # báo ra loa khi có cuộc gọi
          announce_to: media_player.r1_loa
```

Sự kiện `telegram_call_call_state` mang thêm `contact_status` (`friend` có trong danh bạ Telegram · `stranger` · `unknown`), `name` (tên đã lưu), `tags` và `call_type` (`voice`/`video`) để automation chọn người. Trường quy tắc: `answer`, `answer_delay`, `call_types`, `connect_now` (nối ngay), `connect` (nối khi bấm «Người nhà nói trực tiếp»), `video_request` (xin chuyển sang video), `speaker`, `mic`, `mic_url`, `camera`, `volume`, `announce`, `announce_to`, `announce_text` (`{ten}`, `{loai}`), `announce_repeat`, `announce_volume`, `pipeline` (giọng đọc), `greeting` (lời nhắn đọc vào cuộc gọi khi nghe máy) và `hangup_after`. `set_rule` / `remove_rule` / `list_rules` lưu quy tắc sẵn làm **bản dự phòng** khi không automation nào áp quy tắc; `set_contact_tags` gắn thẻ cho người.

Khác Zalo Call: **chưa có trợ lý AI trò chuyện** trong cuộc gọi (gateway chưa đưa tiếng bên kia cho HA) — thay bằng lời nhắn `greeting`; không có chuyển thoại sang video.

**Điều khiển giữa cuộc gọi:** `take_over_call` (nút «Người nhà nói trực tiếp»: nối mic/loa/camera của quy tắc), `stop_call_mic` / `start_call_mic`, `stop_call_video` / `start_call_video` (bật hình cả khi cuộc đang là thoại), `accept_video`, `say_in_call` (đọc một câu vào cuộc gọi), `reject_call`, `end_call`, `alert_call` (gọi, đọc cảnh báo, rồi cúp / nối thiết bị nhà / giữ máy; báo bằng sự kiện `telegram_call_alert_done`). Trên thiết bị có các nút tương ứng, cảm biến *Đã đăng nhập Telegram* và *Trạng thái Telegram*.

**Hình video bên kia ra màn hình / trình duyệt:** gateway nhận khung hình video của người gọi và phát MJPEG tại `GET /api/live/view/<call_id>` (header `Authorization: Bearer <token>`; ảnh chụp tại `/api/live/snapshot/<call_id>`; 409 khi chưa có hình). Tích hợp [Dosen Pro](https://github.com/TriTue2011/Dosen-pro) dùng luồng này để chiếu cuộc gọi Telegram lên màn camera, chỉ với các ID người gọi đã chọn. (Kiểm bằng test và ffmpeg thật; chưa kiểm bằng cuộc gọi video thật.)

**Người nhận:** Cấu hình (bánh răng) → chọn người từ danh bạ Telegram; mỗi người là một thực thể `notify` (thuộc tính `user_id`) để chọn trong blueprint, không phải gõ ID.

#### Blueprint (giống Zalo Call)

Blueprint trong kho [Blueprint](https://github.com/TriTue2011/Blueprint): **Quy tắc cho cuộc gọi đến** (`telegram_call_incoming_rule.yaml`: gọi `apply_call_rule` cho từng cuộc, chọn người từ danh sách thực thể), **Nút bấm mọi loại, nhiều việc** (`nut_bam_nhieu_viec.yaml`: một automation cho mọi nút, thêm việc tuỳ ý như nghe/cúp/gọi đi bằng `telegram_call.*`, chọn theo cảm biến đổi gần nhất). Các blueprint thông báo có Telegram Bot (`alarm_person_camera` 1–4, `calendar_weather_notification`, `ai_creat_image_weather_3`) có thêm ô **«Telegram Calling»** để gửi ảnh, video, tin nhắn qua tài khoản Telegram thật.

Dịch vụ gửi tin nhận cùng mẫu dữ liệu với `telegram_bot` (`config_entry_id` rỗng, `chat_id` hoặc `target`, `message_thread_id` được bỏ qua, `parse_mode` đổi thành `format`) và có `send_media_group` (2-10 ảnh/video trong MỘT album thật, mỗi tệp có `caption` riêng; tệp hoặc âm thanh cũng ghép được với cùng loại. Không ghép được — trộn ảnh với tệp, tin thoại, hoặc chỉ một tệp — thì gửi lần lượt từng tệp, kết quả có `album: true/false`), nên một mẫu dùng được cho cả hai.

Loa, mic, camera, âm lượng mặc định đặt ngay trên thiết bị **Telegram Call** (ô chọn *Loa mặc định*, *Mic*, *Camera*, *Âm lượng cuộc gọi*), `make_call`/`answer_call` chỉ cần ghi đè khi muốn khác. Quy tắc của automation chỉ nối thiết bị đã tích ở `connect_now` / `connect`.

**Ai nghe máy cũng được nối thiết bị.** Tự nghe theo quy tắc thì nối thứ tích ở `connect_now` lúc nghe máy. Nếu cuộc gọi ĐẾN được nghe bằng cách khác (nút loa R1, `answer_call` không kèm thiết bị, hoặc quy tắc tới muộn hơn lúc nghe) thì khi cuộc gọi vào trạng thái *đã nghe máy*, tích hợp nối mic / loa / camera của quy tắc (cả `connect_now` lẫn `connect`, vì người nghe tay chính là người nói) — mỗi cuộc một lần. `answer_call` kèm `mic`/`speaker`/`camera` riêng thì giữ đúng thiết bị đó, không đè. Chưa chọn mic/camera thì mic im lặng và hình đen (không phải âm/hình thử).

### Nhắn tin và nhận tin

| Việc | Dịch vụ | Ví dụ |
|---|---|---|
| Gửi chữ | `send_message` | `message: "*Cảnh báo:* cửa mở"`, `format: markdown`, `reply_to: 123` |
| Gửi ảnh / video / âm thanh / tệp | `send_photo` · `send_video` · `send_audio` · `send_file` | `file: /config/www/anh.jpg` (thư mục cho phép, URL hoặc `media-source://`), `caption: "..."` |
| Gửi tin thoại | `send_voice` | `file: /config/www/loi.mp3` (tự đổi ogg/opus) **hoặc** `message: "Xin chào"` để gateway đọc thành giọng (chỉ người dùng) |
| Sửa · xoá · chuyển tiếp · thả emoji · đã đọc | `edit_message` · `delete_message` · `forward_message` · `add_reaction` · `mark_read` | `message_id: 123`, `emoji: "🔥"` |
| Đọc | `get_history` · `get_chats` · `find_chat` · `get_contacts` | trả dữ liệu cho automation (`response_variable`) |
| Dịch | `translate_text` | `text: "Xin chào"`, `to_language: en` |
| Tải tệp đính kèm về | `download_file` | `file_id` (từ sự kiện), `path: /config/www/nhan.jpg` |

### Hội thoại, bình chọn, danh bạ, nhóm, tài khoản (30 dịch vụ)

Mỗi dịch vụ gọi đúng một hàm của TDLib theo lược đồ chính thức (`td_api.tl`); danh sách nằm ở `custom_components/telegram_call/catalog.py` và `services.yaml` / bản dịch sinh từ đó (`python3 tools/gen_catalog.py`). Dịch vụ nhắm một cuộc trò chuyện nhận `chat_id`, `peer` (@username, +số) hoặc ô «Chọn người từ danh bạ» (`recipient`); dịch vụ nhắm một người nhận `user_id`, `peer` hoặc `recipient`.

| Nhóm | Dịch vụ |
|---|---|
| Hội thoại | `set_mute` (tắt thông báo theo giây) · `pin_chat` · `archive_chat` · `mark_chat_unread` · `set_auto_delete` · `send_typing` · `delete_chat_history` (không hoàn tác) · `rename_chat` · `leave_chat` · `get_chat_info` |
| Tin nhắn | `pin_message` · `unpin_message` · `send_poll` (bình chọn hoặc câu đố) · `vote_poll` · `stop_poll` |
| Người dùng và danh bạ | `find_user` (theo số hoặc @username) · `get_user_info` · `add_contact` · `remove_contact` · `block_user` · `unblock_user` |
| Nhóm | `create_group` · `add_chat_member` · `remove_chat_member` (cấm khoảng 1 phút rồi mời lại được) · `create_invite_link` · `join_chat` (link t.me) |
| Tài khoản | `get_me` · `set_name` · `set_bio` · `set_username` |

**Nhận tin:** sự kiện **`telegram_call_message`** (`chat_id`, `message_id`, `sender`, `kind`, `text`, `file_id`…) và cảm biến *Tin nhắn gần nhất*; ví dụ trả lời tự động:

```yaml
automation:
  - alias: Telegram: nhắn "đèn" thì bật đèn và trả lời
    triggers:
      - trigger: event
        event_type: telegram_call_message
    conditions:
      - "{{ trigger.event.data.text | lower == 'đèn' }}"
    actions:
      - action: light.turn_on
        target: {entity_id: light.phong_khach}
      - action: telegram_call.send_message
        data: {chat_id: "{{ trigger.event.data.chat_id }}", message: "Đã bật đèn", reply_to: "{{ trigger.event.data.message_id }}"}
```

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
