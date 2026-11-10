# Telegram Gateway (Docker) — khung P0 dạng gateway độc lập.
#
# Phạm vi khung này: REST + web UI tĩnh + AccountManager + CallManager +
# AudioHub + EventBridge, ở mức MOCK cho mọi tác vụ cần tài khoản thật.
# KHÔNG đăng nhập thật, KHÔNG gửi tin/gọi thật, KHÔNG dùng credential thật.
# Engine: phương án B (TDLib báo hiệu + NTgCalls media) theo T0-B.
# Đăng nhập thật + glue TDLib→NTgCalls + PCM gọi riêng tư là bước sau (P1/T1).
