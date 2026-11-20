"""Nhật ký chi tiết của WebRTC/NTgCalls ra tệp riêng — để chẩn đoán ước lượng băng thông (vạch sóng điện thoại tụt).

NTgCalls chuyển nhật ký C++ vào hai logger Python «webrtc» và «ntgcalls» nhưng đặt chúng ở CRITICAL nếu chưa ai
đặt mức, nên mặc định không có gì in ra. Bật bằng biến môi trường:
  TELEGRAM_WEBRTC_LOG=info|debug    (trống/khác = tắt — mặc định)
  TELEGRAM_WEBRTC_LOG_LOC=<regex>   (tuỳ chọn: chỉ giữ dòng có «tệp:dòng nội dung» khớp, vd «goog_cc|bwe|rtcp»)
  TELEGRAM_WEBRTC_LOG_MB=20         (trần mỗi tệp; giữ thêm 2 tệp cũ)
Tệp: <DATA_DIR>/calltrace/webrtc.log, quyền 0600 (nhật ký WebRTC có thể chứa ICE ufrag/địa chỉ IP). Giờ địa phương
của container, cùng định dạng HH:MM:SS.mmm với tệp calltrace để ghép theo thời gian.
"""

import logging
import logging.handlers
import os
import re

_MUC = {"info": logging.INFO, "debug": logging.DEBUG}
_TEN = ("webrtc", "ntgcalls")


class _TepRieng(logging.handlers.RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.chmod(self.baseFilename, 0o600)
        return os.fdopen(fd, self.mode, encoding=self.encoding)


def bat(data_dir: str, env=os.environ):
    """Bật theo env; trả đường dẫn tệp hoặc None khi tắt. Gọi một lần lúc khởi động (trước hay sau import ntgcalls đều được)."""
    muc = _MUC.get((env.get("TELEGRAM_WEBRTC_LOG") or "").strip().lower())
    if muc is None:
        return None
    thu_muc = os.path.join(data_dir, "calltrace")
    os.makedirs(thu_muc, exist_ok=True)
    duong = os.path.join(thu_muc, "webrtc.log")
    mb = max(1, int((env.get("TELEGRAM_WEBRTC_LOG_MB") or "20").strip() or 20))
    h = _TepRieng(duong, maxBytes=mb * 1024 * 1024, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(name)s %(levelname)s %(message)s", "%H:%M:%S"))
    loc = (env.get("TELEGRAM_WEBRTC_LOG_LOC") or "").strip()
    if loc:
        mau = re.compile(loc)
        h.addFilter(lambda r: mau.search(r.getMessage()) is not None)
    for ten in _TEN:
        lg = logging.getLogger(ten)
        lg.setLevel(muc)
        lg.addHandler(h)
        lg.propagate = False          # không đổ vào nhật ký chính (docker logs) — có thể hàng nghìn dòng/giây
    return duong
