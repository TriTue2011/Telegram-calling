"""Nhật ký CHI TIẾT từng cuộc gọi Telegram (17/11/2026), cùng tinh thần và định dạng với Zalo (zalo_pc_app/call_trace.py).

Mỗi cuộc một tệp `<DATA_DIR>/calltrace/<id>.log` (volume dữ liệu của container), cuộc TỐT chỉ giữ ``KEEP`` cuộc gần nhất (mặc định 3), cuộc LỖI (tệp đánh dấu `<id>.loi`) giữ tới ``KEEP_BAD`` (mặc định 30); KHÔNG đi vào cơ sở dữ liệu của Home Assistant.
Dòng: `HH:MM:SS.mmm +T.TTTs <loại> nội dung` (T = giây từ dòng đầu của cuộc gọi). Gồm:
  - `su-kien`: MỌI dòng của LiveCall.log (TDLib: trạng thái/lý do kết thúc/emoji/máy chủ; NTgCalls: kết nối, khung đầu, nguồn gửi chậm…), không bị cắt 200 dòng như bộ nhớ;
  - `goi-tx` / `goi-rx`: TỪNG gói BÁO HIỆU giữa TDLib và NTgCalls (độ dài, 12 byte đầu hex). Media (SRTP/ICE) chạy trong thư viện NTgCalls nên Python không thấy từng gói:
    thay bằng số liệu mạng của container mỗi giây (`mang-1s`: gói/byte nhận-gửi, lỗi, rớt của giao diện mạng; `udp-1s`: gói rớt ở ổ UDP của tiến trình);
  - `dem-1s`: mỗi giây HIỆU của mọi bộ đếm cuộc gọi (báo hiệu tx/rx, khung tiếng vào, byte, khung hình nhận/rớt/hỏng…); `dem-tong`, `tai-nguyen` (CPU %, RSS, luồng, tải) mỗi 5 giây;
  - `xu-huong` (mỗi 5 giây): tốc độ nhận/gửi trung bình 5 giây, đỉnh và TỈ LỆ so với đỉnh — để thấy «vạch sóng tụt dần» (bên kia hạ bitrate vì họ đo mất gói/trễ) thay vì chỉ biết «tín hiệu yếu»;
  - cuộc LỖI = có đánh dấu tường minh (`mark_bad`: NTgCalls FAILED/TIMEOUT, nguồn gửi chậm), hoặc tốc độ nhận tụt dưới 60% đỉnh liên tục ≥ 5 giây (đỉnh ≥ 2 KB/s), hoặc có gói rớt/lỗi ở giao diện mạng hay ổ UDP khi đang gọi;
  - `ntg`: mỗi 3 giây trạng thái NTgCalls (CPU, chế độ kết nối, trạng thái) — luôn ghi, không cần TELEGRAM_DEBUG;
  - phần mở đầu: phiên bản, mọi biến TELEGRAM_* (trừ giá trị có PASSWORD/TOKEN/KEY/SECRET/HASH), phiên bản thư viện; phần kết: tổng kết toàn bộ (info) của cuộc gọi.
Không bao giờ ghi bí mật; chuỗi dài giống token bị che; lỗi của bộ ghi bị nuốt (chẩn đoán không được làm hỏng cuộc gọi). Tắt: TELEGRAM_CALLTRACE_KEEP=0."""
import json
import os
import re
import threading
import time

KEEP = int(os.environ.get("TELEGRAM_CALLTRACE_KEEP", "3") or 0)                 # số cuộc TỐT giữ lại; 0 = tắt hẳn
KEEP_BAD = int(os.environ.get("TELEGRAM_CALLTRACE_KEEP_BAD", "30") or 30)       # số cuộc LỖI giữ lại
MAX_BYTES = int(os.environ.get("TELEGRAM_CALLTRACE_MAX_KB", "6144") or 6144) * 1024
SAMPLE_SECONDS = 1.0
SNAP_EVERY = 5
TAIL_AFTER_END = 6.0
BAD_SUFFIX = ".loi"
TREND_WINDOW = 5                 # giây: cửa sổ trung bình tốc độ nhận/gửi
TREND_MIN_PEAK = 2048            # B/s: đỉnh nhận thấp hơn thì không đủ dữ liệu để nói «tụt»
TREND_LOW_RATIO = 0.60
TREND_LOW_SECONDS = 5
MAX_SAMPLER_SECONDS = 4 * 3600
_SECRET_NAME = re.compile(r"PASSWORD|TOKEN|KEY|SECRET|HASH|COOKIE", re.I)
_LONG = re.compile(r"[A-Za-z0-9+/=_\-]{40,}")

_lock = threading.RLock()
_dir = None
_providers = {}
_traces = {}


def configure(data_dir, stats=None, version="", extra_header=None):
    """stats(call_id) -> dict số liệu (số đếm cộng dồn) hoặc None khi cuộc gọi không còn; extra_header(call_id) -> chuỗi mô tả."""
    global _dir, _providers
    _dir = os.path.join(data_dir, "calltrace") if data_dir else None
    _providers = {"stats": stats, "version": version, "extra": extra_header}


def enabled():
    return KEEP > 0 and _dir is not None


def mask(text):
    return _LONG.sub("<ẩn>", str(text))


class _Trace:
    def __init__(self, call_id, path):
        self.call_id, self.path, self.t0 = call_id, path, time.time()
        self.size = self.lines = 0
        self.full = True
        self.ended_at = None
        self.closed = False
        self.bad = None
        self.rx_hist, self.tx_hist = [], []
        self.rx_peak = self.tx_peak = 0.0
        self.low_streak = 0
        self.last = {}
        self.last_net = None
        self.last_cpu = None
        self.pkt = {"tx": 0, "rx": 0}
        self.fh = open(path, "a", encoding="utf-8", buffering=1)
        self.sampler = None

    def write(self, kind, text, force=False):
        now = time.time()
        if not self.full and not force:
            return
        line = "%s.%03d +%.3fs %s %s\n" % (time.strftime("%H:%M:%S", time.localtime(now)), int((now % 1) * 1000), now - self.t0, kind, text)
        if self.size + len(line) > MAX_BYTES and not force:
            if self.full:
                self.full = False
                line = "%s.%03d +%.3fs trần dung lượng %d KB: ngừng ghi chi tiết (chỉ còn dòng kết thúc)\n" % (
                    time.strftime("%H:%M:%S", time.localtime(now)), int((now % 1) * 1000), now - self.t0, MAX_BYTES // 1024)
            else:
                return
        try:
            self.fh.write(line)
            self.size += len(line.encode("utf-8"))
            self.lines += 1
        except (OSError, ValueError):
            self.full = False

    def close(self):
        self.closed = True
        try:
            self.fh.close()
        except (OSError, ValueError):
            pass


def _header(t):
    env = ["%s=%s" % (k, mask(os.environ[k])[:60]) for k in sorted(os.environ) if k.startswith("TELEGRAM_") and not _SECRET_NAME.search(k)]
    t.write("mo-dau", "call=%s gateway=%s pid=%d keep=%d max_kb=%d" % (t.call_id, _providers.get("version", ""), os.getpid(), KEEP, MAX_BYTES // 1024))
    if env:
        t.write("moi-truong", " ".join(env))
    fn = _providers.get("extra")
    if callable(fn):
        try:
            t.write("cau-hinh", mask(fn(t.call_id))[:900])
        except Exception as exc:  # noqa: BLE001
            t.write("cau-hinh", "loi: %s" % exc)


def _bad_path(log_path):
    return log_path[:-4] + BAD_SUFFIX


def _prune():
    """Cuộc TỐT: giữ KEEP cuộc mới nhất; cuộc LỖI (có tệp .loi): giữ KEEP_BAD. Cuộc chưa chốt kết luận (còn ghi) không tính vào suất «tốt» và không bị xoá."""
    try:
        pending = {t.path for t in _traces.values() if not t.closed}
        files = [os.path.join(_dir, f) for f in os.listdir(_dir) if f.endswith(".log")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        good = [p for p in files if not os.path.exists(_bad_path(p)) and p not in pending]
        bad = [p for p in files if os.path.exists(_bad_path(p))]
        for p in good[KEEP:] + bad[KEEP_BAD:]:
            for victim in (p, _bad_path(p)):
                try:
                    os.remove(victim)
                except OSError:
                    pass
    except OSError:
        pass


def mark_bad(call_id, reason):
    """Đánh dấu tường minh cuộc này LỖI (lý do đầu tiên thắng); tệp `.loi` được ghi ngay để cuộc đang chạy cũng không bị dọn."""
    if not enabled():
        return
    try:
        t = _get(call_id)
        if t is not None and t.bad is None:
            t.bad = str(reason)[:80]
            t.write("danh-dau-loi", mask(t.bad), force=True)
            with open(_bad_path(t.path), "w", encoding="utf-8") as fh:
                fh.write(t.bad + "\n")
    except Exception:  # noqa: BLE001
        pass


def _avg(hist, n=TREND_WINDOW):
    tail = hist[-n:]
    return sum(tail) / float(len(tail)) if tail else 0.0


def _trend(t, tick, rx_b, tx_b):
    """Mỗi giây một mẫu tốc độ (B/s); đỉnh = trung bình 5 s cao nhất; tụt liên tục dưới 30% đỉnh ≥ 5 s thì đánh dấu LỖI."""
    t.rx_hist.append(rx_b)
    t.tx_hist.append(tx_b)
    del t.rx_hist[:-600], t.tx_hist[:-600]
    rx, tx = _avg(t.rx_hist), _avg(t.tx_hist)
    t.rx_peak, t.tx_peak = max(t.rx_peak, rx), max(t.tx_peak, tx)
    low = t.rx_peak >= TREND_MIN_PEAK and rx < t.rx_peak * TREND_LOW_RATIO and t.ended_at is None
    t.low_streak = t.low_streak + 1 if low else 0
    if tick % SNAP_EVERY == 0:
        t.write("xu-huong", "nhan=%.1fKB/s dinh_nhan=%.1fKB/s con_lai=%s gui=%.1fKB/s dinh_gui=%.1fKB/s con_lai=%s" % (
            rx / 1024.0, t.rx_peak / 1024.0, ("%d%%" % (rx / t.rx_peak * 100)) if t.rx_peak else "-",
            tx / 1024.0, t.tx_peak / 1024.0, ("%d%%" % (tx / t.tx_peak * 100)) if t.tx_peak else "-"))
    if t.low_streak >= TREND_LOW_SECONDS and t.bad is None:
        mark_bad(t.call_id, "toc_do_nhan_tut_duoi_%d%%_dinh" % int(TREND_LOW_RATIO * 100))


def _get(call_id, create=True):
    cid = str(call_id)
    with _lock:
        t = _traces.get(cid)
        if t is not None or not create or not enabled():
            return t
        try:
            os.makedirs(_dir, exist_ok=True)
            t = _Trace(cid, os.path.join(_dir, cid + ".log"))
        except OSError:
            return None
        _traces[cid] = t
        for k in [k for k, v in _traces.items() if v.ended_at is not None and k != cid and time.time() - v.ended_at > TAIL_AFTER_END + 2]:
            _traces.pop(k).close()
        _header(t)
        _prune()
        _udp_start()
        _start_sampler(t)
        return t


def line(call_id, kind, text):
    """Một dòng tuỳ ý (tự tạo tệp ở dòng đầu tiên của cuộc gọi)."""
    if not enabled():
        return
    try:
        t = _get(call_id)
        if t is not None:
            t.write(kind, mask(text))
    except Exception:  # noqa: BLE001 - chẩn đoán không được làm hỏng cuộc gọi
        pass


def packet(call_id, direction, data):
    """Một gói báo hiệu: hướng tx|rx, độ dài, 12 byte đầu hex."""
    if not enabled():
        return
    try:
        t = _get(call_id)
        if t is None:
            return
        t.pkt[direction] = t.pkt.get(direction, 0) + 1
        t.write("goi-" + direction, "#%d baohieu len=%d hex=%s" % (t.pkt[direction], len(data), bytes(data[:12]).hex()))
    except Exception:  # noqa: BLE001
        pass


def finish(call_id, summary=""):
    """Cuộc gọi kết thúc: ghi tổng kết đầy đủ; bộ lấy mẫu còn chạy TAIL_AFTER_END giây rồi tự đóng tệp."""
    if not enabled():
        return
    try:
        t = _get(call_id, create=False)
        if t is not None and t.ended_at is None:
            rx = _avg(t.rx_hist)
            if t.bad is None and t.rx_peak >= TREND_MIN_PEAK and rx < t.rx_peak * TREND_LOW_RATIO:
                mark_bad(call_id, "ket_thuc_khi_toc_do_nhan_con_%d%%_dinh" % int(rx / t.rx_peak * 100))
            t.write("tong-ket", mask(summary)[:3000], force=True)
            t.ended_at = time.time()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- số liệu hệ thống
def _net_totals():
    """Tổng gói/byte/lỗi/rớt của MỌI giao diện trừ lo (container gateway chỉ chạy gateway, nên đây ≈ lưu lượng của cuộc gọi) + rớt ở ổ UDP của tiến trình."""
    rx_b = rx_p = rx_e = rx_d = tx_b = tx_p = tx_e = tx_d = 0
    with open("/proc/net/dev") as f:
        for ln in f.read().splitlines()[2:]:
            name, _, rest = ln.partition(":")
            if name.strip() == "lo":
                continue
            v = [int(x) for x in rest.split()]
            rx_b += v[0]; rx_p += v[1]; rx_e += v[2]; rx_d += v[3]
            tx_b += v[8]; tx_p += v[9]; tx_e += v[10]; tx_d += v[11]
    udp_drops = 0
    for p in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(p) as f:
                for ln in f.read().splitlines()[1:]:
                    cols = ln.split()
                    if len(cols) >= 13:
                        udp_drops += int(cols[12])
        except (OSError, ValueError):
            pass
    return {"rx_pkt": rx_p, "rx_byte": rx_b, "rx_err": rx_e, "rx_drop": rx_d, "tx_pkt": tx_p, "tx_byte": tx_b, "tx_err": tx_e, "tx_drop": tx_d, "udp_drop": udp_drops}


def _proc_stats(t):
    try:
        with open("/proc/self/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])
        now = time.monotonic()
        cpu = None
        if t.last_cpu is not None and now > t.last_cpu[1]:
            cpu = (ticks - t.last_cpu[0]) / float(os.sysconf("SC_CLK_TCK")) / (now - t.last_cpu[1]) * 100.0
        t.last_cpu = (ticks, now)
        with open("/proc/self/status") as f:
            st = f.read()
        rss = re.search(r"VmRSS:\s+(\d+)", st)
        thr = re.search(r"Threads:\s+(\d+)", st)
        la = os.getloadavg()
        return "cpu=%s rss=%sMB thr=%s load=%.2f/%.2f/%.2f" % ("%.0f%%" % cpu if cpu is not None else "-", int(rss.group(1)) // 1024 if rss else "-", thr.group(1) if thr else "-", la[0], la[1], la[2])
    except (OSError, ValueError, IndexError, AttributeError):
        return "tai-nguyen: khong doc duoc"


# ---------------------------------------------------------------- đếm RIÊNG lưu lượng UDP (media Telegram)
# Giao diện mạng của container còn chở RTSP camera (TCP, vào), luồng loa/mic HTTP tới R1 và gọi API: đếm cả giao diện làm «tốc độ nhận» lẫn các thứ đó
# (18/11: nhận 553 KB/s trong khi điện thoại chỉ gửi ≤150 KB/s). Media Telegram đi bằng UDP tới relay/điện thoại ⇒ chỉ đếm UDP (bỏ DNS cổng 53).
_udp_lock = threading.Lock()
_udp = {"rx_byte": 0, "tx_byte": 0, "rx_pkt": 0, "tx_pkt": 0}
_udp_state = {"thread": None, "ok": None}
_udp_last = {"rx": None, "tx": None}                       # thời điểm gói trước (giây, đồng hồ hạt nhân) theo hướng
_udp_win = {"rx": [0, 0.0, 0], "tx": [0, 0.0, 0]}          # cửa sổ 1 s: [số khoảng, khoảng lớn nhất (s), số khoảng >40 ms]
JITTER_GAP_S = 0.04
# Phân loại gói UDP theo ĐẦU gói (phần không mã hoá): RTP (đếm theo SSRC: mất gói theo số thứ tự, gói đến lệch thứ tự), RTCP (đếm theo loại gói đầu: 200 SR,
# 201 RR, 205 phản hồi truyền tải/NACK/TWCC, 206 phản hồi tải trọng/PLI/REMB — nội dung SRTCP bị mã hoá nhưng đầu thì rõ), STUN, DTLS. Hai chiều tách riêng:
# «rx» = điện thoại → gateway (chất lượng đường GỬI LÊN của điện thoại), «tx» = gateway → điện thoại; số RTCP loại 205/206 ở «rx» cho biết điện thoại đang
# XIN gửi lại gói / xin khung chính vì mất gói ở chiều gateway → điện thoại.
_rtp_win = {"rx": None, "tx": None}                       # cửa sổ 1 s: {"rtp": {ssrc: [gói, byte, mất, lệch]}, "rtcp": {pt: n}, "stun": n, "dtls": n, "khac": {byte0: n}}
_rtp_hi = {}                                               # (hướng, ssrc) -> số thứ tự cao nhất đã thấy (16 bit)
_rtp_tot = {}                                              # (hướng, ssrc) -> [gói, byte, mất, lệch] CỘNG DỒN
RTP_MAX_SSRC = 64
STUN_COOKIE = b"\x21\x12\xa4\x42"


def _win_new():
    return {"rtp": {}, "rtcp": {}, "stun": 0, "dtls": 0, "khac": {}}


_rtp_win.update({"rx": _win_new(), "tx": _win_new()})


def classify_udp(payload):
    """('rtp', ssrc, seq) | ('rtcp', pt) | ('stun',) | ('dtls',) | ('khac', byte0) | None. Tự bỏ phần bọc ChannelData 4 byte của TURN (0x4000–0x7FFF)."""
    if len(payload) < 4:
        return None
    b0 = payload[0]
    if 0x40 <= b0 <= 0x7F and len(payload) >= 16 and int.from_bytes(payload[2:4], "big") <= len(payload) - 4:
        payload, b0 = payload[4:], payload[4]
    if b0 <= 1 and payload[4:8] == STUN_COOKIE:
        return ("stun",)
    if 20 <= b0 <= 63:
        return ("dtls",)
    if 0x80 <= b0 <= 0xBF and len(payload) >= 12:
        if 192 <= payload[1] <= 223:
            return ("rtcp", payload[1])
        return ("rtp", int.from_bytes(payload[8:12], "big"), int.from_bytes(payload[2:4], "big"))
    return ("khac", b0)


def _rtp_account(key, payload, size):
    """Cộng một gói vào cửa sổ + cộng dồn (gọi trong _udp_lock)."""
    c = classify_udp(payload)
    if c is None:
        return
    w = _rtp_win[key]
    if c[0] == "rtp":
        ssrc, seq = c[1], c[2]
        e = w["rtp"].setdefault(ssrc, [0, 0, 0, 0])
        tot = _rtp_tot.get((key, ssrc))
        if tot is None and len(_rtp_tot) < RTP_MAX_SSRC:
            tot = _rtp_tot[(key, ssrc)] = [0, 0, 0, 0]
        e[0] += 1
        e[1] += size
        hi = _rtp_hi.get((key, ssrc))
        missing = late = 0
        if hi is None:
            _rtp_hi[(key, ssrc)] = seq
        else:
            d = (seq - hi) & 0xFFFF
            if 1 <= d < 0x8000:
                missing = d - 1
                _rtp_hi[(key, ssrc)] = seq
            elif d >= 0x8000:
                late = 1                               # gói cũ đến muộn (đã tính vào «mất» lúc nhảy qua nó)
        e[2] += missing
        e[3] += late
        if tot is not None:
            tot[0] += 1
            tot[1] += size
            tot[2] += missing
            tot[3] += late
    elif c[0] == "rtcp":
        w["rtcp"][c[1]] = w["rtcp"].get(c[1], 0) + 1
    elif c[0] in ("stun", "dtls"):
        w[c[0]] += 1
    else:
        w["khac"][c[1]] = w["khac"].get(c[1], 0) + 1


def _rtp_window_take():
    """Lấy (và xoá) cửa sổ 1 s vừa qua của hai hướng."""
    with _udp_lock:
        out = {k: v for k, v in _rtp_win.items()}
        for k in _rtp_win:
            _rtp_win[k] = _win_new()
    return out


def rtp_line(win):
    """Một dòng nhật ký cho cửa sổ lấy từ _rtp_window_take(); chuỗi rỗng nếu không có gì."""
    parts = []
    for k in ("rx", "tx"):
        w = win[k]
        seg = []
        for ssrc, (n, byt, miss, late) in sorted(w["rtp"].items(), key=lambda kv: -kv[1][1])[:3]:
            seg.append("%08x:%dgoi/%dB(tb%dB) mat=%d lech=%d" % (ssrc, n, byt, byt // max(1, n), max(0, miss - late), late))
        if w["rtcp"]:
            seg.append("rtcp[" + ",".join("%d×%d" % (pt, n) for pt, n in sorted(w["rtcp"].items())) + "]")
        for name in ("stun", "dtls"):
            if w[name]:
                seg.append("%s=%d" % (name, w[name]))
        if w["khac"]:
            seg.append("khac[" + ",".join("0x%02x×%d" % (b, n) for b, n in sorted(w["khac"].items())) + "]")
        if seg:
            parts.append("%s: %s" % (k, " ".join(seg)))
    return " | ".join(parts)


def rtp_totals_line():
    """Số CỘNG DỒN theo (hướng, SSRC): gói, byte, mất theo số thứ tự (đã trừ gói đến muộn), lệch thứ tự."""
    with _udp_lock:
        items = sorted(_rtp_tot.items(), key=lambda kv: -kv[1][1])[:6]
    return " ".join("%s:%08x=%dgoi/%dB mat=%d lech=%d" % (k, ssrc, v[0], v[1], max(0, v[2] - v[3]), v[3]) for (k, ssrc), v in items)
_own_ports = {"set": None, "at": 0.0}                      # cổng UDP cục bộ của CHÍNH tiến trình (None = chưa biết: đếm mọi UDP)


def host_network():
    """Container dùng mạng của máy chủ (network_mode: host)? Khi đó giao diện mạng và ổ UDP thấy là của CẢ máy chủ, không phải riêng gateway."""
    try:
        names = set(os.listdir("/sys/class/net"))
    except OSError:
        return False
    return "docker0" in names or any(n.startswith("br-") for n in names)


def _own_udp_ports():
    """Cổng UDP cục bộ mà tiến trình này đang mở (đọc inode ổ cắm từ /proc/self/fd rồi tra /proc/net/udp[6])."""
    inodes = set()
    try:
        for fd in os.listdir("/proc/self/fd"):
            try:
                tgt = os.readlink("/proc/self/fd/" + fd)
            except OSError:
                continue
            if tgt.startswith("socket:["):
                inodes.add(tgt[8:-1])
    except OSError:
        return None
    ports = set()
    for path in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(path) as f:
                for ln in f.read().splitlines()[1:]:
                    c = ln.split()
                    if len(c) > 9 and c[9] in inodes:
                        ports.add(int(c[1].rsplit(":", 1)[1], 16))
        except (OSError, ValueError, IndexError):
            return None
    return ports


def _ports_for_filter():
    """Chỉ lọc theo cổng khi chạy mạng máy chủ (bridge: mọi UDP của container đều là của gateway). Làm mới ≤ 2 lần/giây."""
    if not host_network():
        return None
    now = time.monotonic()
    if _own_ports["set"] is None or now - _own_ports["at"] > 0.5:
        _own_ports["set"], _own_ports["at"] = _own_udp_ports(), now
    return _own_ports["set"]


def _udp_account(frame, pkttype, ts=None):
    """Cộng một khung Ethernet/IPv4/UDP vào bộ đếm. pkttype 4 (PACKET_OUTGOING) = gói ra. ts = giờ hạt nhân của gói (giây) để đo KHOẢNG CÁCH giữa các gói.
    Trả True nếu tính."""
    if len(frame) < 42 or frame[12:14] != b"\x08\x00" or frame[23] != 17:
        return False
    ihl = (frame[14] & 15) * 4
    if len(frame) < 14 + ihl + 8:
        return False
    sport, dport = int.from_bytes(frame[14 + ihl:16 + ihl], "big"), int.from_bytes(frame[16 + ihl:18 + ihl], "big")
    if 53 in (sport, dport):
        return False
    own = _ports_for_filter() if _udp_state.get("filter", True) else None
    if own is not None and sport not in own and dport not in own:
        return False                                                       # lưu lượng của tiến trình khác trên máy chủ
    n = max(0, len(frame) - 14 - ihl - 8)
    key = "tx" if pkttype == 4 else "rx"
    with _udp_lock:
        _udp[key + "_byte"] += n
        _udp[key + "_pkt"] += 1
        try:
            _rtp_account(key, frame[14 + ihl + 8:], n)
        except Exception:  # noqa: BLE001 - phân loại hỏng không được làm mất bộ đếm chính
            pass
        if ts is not None:
            last = _udp_last[key]
            _udp_last[key] = ts
            if last is not None and ts >= last:
                w = _udp_win[key]
                gap = ts - last
                w[0] += 1
                w[1] = max(w[1], gap)
                w[2] += 1 if gap > JITTER_GAP_S else 0
    return True


def _udp_window_take():
    """Lấy (và xoá) thống kê khoảng cách gói của 1 giây vừa qua: {rx|tx: (số khoảng, khoảng lớn nhất ms, số khoảng >40 ms)}."""
    with _udp_lock:
        out = {k: (v[0], v[1] * 1000.0, v[2]) for k, v in _udp_win.items()}
        for v in _udp_win.values():
            v[0], v[1], v[2] = 0, 0.0, 0
    return out


def _udp_loop():
    import ctypes
    import fcntl
    import socket
    import struct
    try:
        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.ntohs(3))
        prog = [(0x28, 0, 0, 12), (0x15, 0, 3, 0x0800), (0x30, 0, 0, 23), (0x15, 0, 1, 17), (0x06, 0, 0, 65535), (0x06, 0, 0, 0)]      # chỉ IPv4/UDP
        buf = ctypes.create_string_buffer(b"".join(__import__("struct").pack("HBBI", *i) for i in prog), 8 * len(prog))

        class _Fprog(ctypes.Structure):
            _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]
        fp = _Fprog(len(prog), ctypes.cast(buf, ctypes.c_void_p))
        sock.setsockopt(socket.SOL_SOCKET, 26, bytes(ctypes.string_at(ctypes.addressof(fp), ctypes.sizeof(fp))))     # SO_ATTACH_FILTER
        sock.settimeout(1.0)
        _udp_state["ok"] = True
    except Exception:  # noqa: BLE001 - không có quyền raw: dùng số đo giao diện mạng
        _udp_state["ok"] = False
        return
    while True:
        try:
            data, addr = sock.recvfrom(65535)
        except socket.timeout:
            continue
        except OSError:
            _udp_state["ok"] = False
            return
        try:
            ts = None
            try:
                sec, usec = struct.unpack("ll", fcntl.ioctl(sock.fileno(), 0x8906, b"\0" * 16)[:16])        # SIOCGSTAMP: giờ hạt nhân của gói vừa nhận
                ts = sec + usec / 1e6
            except Exception:  # noqa: BLE001
                pass
            _udp_account(data, addr[2], ts)
        except Exception:  # noqa: BLE001
            pass


def _udp_totals():
    """Số đếm UDP cộng dồn {rx_byte, tx_byte, rx_pkt, tx_pkt}; None nếu không đếm được (thiếu quyền raw)."""
    with _udp_lock:
        return dict(_udp) if _udp_state["ok"] else None


def _udp_start():
    if _udp_state["thread"] is None:
        _udp_state["thread"] = threading.Thread(target=_udp_loop, daemon=True, name="calltrace-udp")
        _udp_state["thread"].start()


def _cgroup_cpu():
    """Số lần / thời gian container bị BÓP CPU (hạn mức `cpus` của docker). Bị bóp thì gói UDP nhận bị trễ theo cụm → bên gửi (điện thoại) đo độ trễ tăng
    và tự hạ tốc độ (đồ thị tụt dần, vạch sóng giảm). {} nếu không đọc được (cgroup v1 / không có)."""
    try:
        with open("/sys/fs/cgroup/cpu.stat") as f:
            kv = dict(ln.split()[:2] for ln in f.read().splitlines() if len(ln.split()) >= 2)
        return {"nr_throttled": int(kv["nr_throttled"]), "throttled_usec": int(kv["throttled_usec"]), "cpu_usec": int(kv["usage_usec"])}
    except (OSError, KeyError, ValueError):
        return {}


def _diff(t, key, cur):
    out = []
    prev = t.last.get(key, {})
    for k in sorted(cur):
        v = cur[k]
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            d = v - prev.get(k, 0)
            if d:
                out.append("%s%+d" % (k, d) if isinstance(d, int) else "%s%+.2f" % (k, d))
    t.last[key] = {k: v for k, v in cur.items() if isinstance(v, (int, float))}
    return out


def sample_once(t, tick):
    """MỘT lần lấy mẫu (tách riêng cho test)."""
    fn = _providers.get("stats")
    cur = fn(t.call_id) if callable(fn) else None
    if cur is not None:
        d = _diff(t, "call", cur)
        if d:
            t.write("dem-1s", " ".join(d))
        if tick % SNAP_EVERY == 0:
            t.write("dem-tong", " ".join("%s=%s" % (k, cur[k]) for k in sorted(cur) if cur[k] not in (0, None, "")))
    try:
        net = _net_totals()
        prev = dict(t.last.get("net", {}))
        nd = _diff(t, "net", {k: v for k, v in net.items() if k != "udp_drop"})
        if nd:
            t.write("mang-1s", " ".join(nd))
        udp = _udp_totals()
        if udp is not None:
            uprev = dict(t.last.get("udpmeter", {}))
            ud2 = _diff(t, "udpmeter", udp)
            if ud2:
                t.write("udp-flujo-1s", " ".join(ud2))
            rl = rtp_line(_rtp_window_take())
            if rl:
                t.write("rtp-1s", rl)
            if tick % SNAP_EVERY == 0:
                rt = rtp_totals_line()
                if rt:
                    t.write("rtp-tong", rt)
            jit = _udp_window_take()
            if any(v[0] for v in jit.values()):
                t.write("udp-khoang-1s", " ".join("%s_khoang_max=%.0fms %s_tren40ms=%d/%d" % (k, v[1], k, v[2], v[0]) for k, v in sorted(jit.items()) if v[0]))
        if prev and t.ended_at is None:
            if udp is not None and uprev:
                _trend(t, tick, udp["rx_byte"] - uprev.get("rx_byte", 0), udp["tx_byte"] - uprev.get("tx_byte", 0))     # chỉ media UDP
            elif udp is None:
                _trend(t, tick, net["rx_byte"] - prev.get("rx_byte", 0), net["tx_byte"] - prev.get("tx_byte", 0))
            if t.bad is None and not host_network() and any(net[k] - prev.get(k, 0) > 0 for k in ("rx_err", "rx_drop", "tx_err", "tx_drop")):
                mark_bad(t.call_id, "goi_rot_hoac_loi_giao_dien_mang")             # (mạng máy chủ: số rớt là của cả máy, không quy cho cuộc gọi)
        ud_prev = t.last.get("udp", {}).get("udp_drop")
        ud = _diff(t, "udp", {"udp_drop": net["udp_drop"]})
        if ud:
            t.write("udp-1s", " ".join(ud))
            if ud_prev is not None and t.ended_at is None and t.bad is None and not host_network() and net["udp_drop"] > ud_prev:
                mark_bad(t.call_id, "o_udp_rot_goi")
    except (OSError, ValueError, IndexError):
        if tick % SNAP_EVERY == 0:
            t.write("mang-1s", "khong doc duoc /proc/net")
    cg = _cgroup_cpu()
    if cg:
        prev_cg = dict(t.last.get("cg", {}))
        d = _diff(t, "cg", cg)
        if prev_cg and (cg["nr_throttled"] > prev_cg.get("nr_throttled", 0)):
            t.write("cpu-bi-bop", " ".join(d))
            if t.bad is None and t.ended_at is None:
                mark_bad(t.call_id, "container_bi_bop_cpu")
    if tick % SNAP_EVERY == 0:
        t.write("tai-nguyen", _proc_stats(t))


def _sampler(t):
    tick = 0
    started = time.time()
    while True:
        time.sleep(SAMPLE_SECONDS)
        if time.time() - started > MAX_SAMPLER_SECONDS:
            break
        try:
            sample_once(t, tick)
        except Exception as exc:  # noqa: BLE001
            t.write("loi-bo-ghi", "%s: %s" % (type(exc).__name__, str(exc)[:80]))
        tick += 1
        if t.ended_at is not None and time.time() - t.ended_at > TAIL_AFTER_END:
            break
    try:
        t.write("ket-thuc-nhat-ky", "dong=%d byte=%d goi_tx=%d goi_rx=%d ket_luan=%s" % (t.lines, t.size, t.pkt["tx"], t.pkt["rx"], ("LOI:" + t.bad) if t.bad else "tot"), force=True)
    finally:
        t.close()
    _prune()


def _start_sampler(t):
    if SAMPLE_SECONDS <= 0:
        return
    th = threading.Thread(target=_sampler, args=(t,), daemon=True, name="calltrace-%s" % t.call_id)
    t.sampler = th
    th.start()


# ---------------------------------------------------------------- đọc ra (API)
def list_traces():
    out = []
    if _dir is None:
        return out
    try:
        for f in os.listdir(_dir):
            if f.endswith(".log") and re.fullmatch(r"[0-9a-f]{6,32}", f[:-4]):
                st = os.stat(os.path.join(_dir, f))
                out.append({"id": f[:-4], "bytes": st.st_size, "modified": int(st.st_mtime), "_m": st.st_mtime, "loi": _read_reason(os.path.join(_dir, f)),
                            "live": f[:-4] in _traces and _traces[f[:-4]].ended_at is None})
    except OSError:
        pass
    out.sort(key=lambda r: r["_m"], reverse=True)
    for r in out:
        r.pop("_m")
    return out


def _read_reason(log_path):
    try:
        with open(_bad_path(log_path), encoding="utf-8") as fh:
            return fh.read().strip() or "loi"
    except OSError:
        return None


def read(call_id):
    """Nội dung nhật ký; chỉ nhận id dạng hex (không đường dẫn tuỳ ý). None nếu không có."""
    cid = str(call_id)
    if _dir is None or not re.fullmatch(r"[0-9a-f]{6,32}", cid):
        return None
    try:
        with open(os.path.join(_dir, cid + ".log"), "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def reset_for_tests():
    with _udp_lock:
        for k in _rtp_win:
            _rtp_win[k] = _win_new()
        _rtp_hi.clear()
        _rtp_tot.clear()
    with _lock:
        for t in _traces.values():
            t.ended_at = t.ended_at or time.time()
            t.close()
        _traces.clear()
