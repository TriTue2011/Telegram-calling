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
    """Cuộc TỐT: giữ KEEP cuộc mới nhất; cuộc LỖI (có tệp .loi): giữ KEEP_BAD. Cuộc vừa kết thúc mà bộ lấy mẫu còn ghi đuôi thì chưa đụng."""
    try:
        live = {t.path for t in _traces.values() if t.ended_at is not None and not t.closed}
        files = [os.path.join(_dir, f) for f in os.listdir(_dir) if f.endswith(".log")]
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        good = [p for p in files if not os.path.exists(_bad_path(p))]
        bad = [p for p in files if os.path.exists(_bad_path(p))]
        for p in [q for q in good[KEEP:] if q not in live] + bad[KEEP_BAD:]:
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
        if prev and t.ended_at is None:
            _trend(t, tick, net["rx_byte"] - prev.get("rx_byte", 0), net["tx_byte"] - prev.get("tx_byte", 0))
            if t.bad is None and any(net[k] - prev.get(k, 0) > 0 for k in ("rx_err", "rx_drop", "tx_err", "tx_drop")):
                mark_bad(t.call_id, "goi_rot_hoac_loi_giao_dien_mang")
        ud_prev = t.last.get("udp", {}).get("udp_drop")
        ud = _diff(t, "udp", {"udp_drop": net["udp_drop"]})
        if ud:
            t.write("udp-1s", " ".join(ud))
            if ud_prev is not None and t.ended_at is None and t.bad is None and net["udp_drop"] > ud_prev:
                mark_bad(t.call_id, "o_udp_rot_goi")
    except (OSError, ValueError, IndexError):
        if tick % SNAP_EVERY == 0:
            t.write("mang-1s", "khong doc duoc /proc/net")
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
    with _lock:
        for t in _traces.values():
            t.ended_at = t.ended_at or time.time()
            t.close()
        _traces.clear()
