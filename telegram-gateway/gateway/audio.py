"""AudioHub — interface PCM chung, engine chỉ thấy byte PCM.

Nguyên tắc (giữ nguyên từ thiết kế): gateway không bao giờ đưa mic/loa
vật lý vào trong engine; engine chỉ thấy byte PCM, gateway lo chuyển
đổi giữa byte PCM và endpoint (HA, R1, USB, file, pipe). Hai chiều chạy
ĐỒNG THỜI, full-duplex.

Định dạng engine — CẢNH BÁO MỨC BẰNG CHỨNG: tiền lệ group call của
NTgCalls là s16le 48 kHz; định dạng chính xác cho GỌI RIÊNG TƯ vẫn
CHƯA được xác minh (chờ T1 — cuộc gọi thật). Hằng dưới đây là điểm
xuất phát, KHÔNG phải kết luận cho gọi riêng tư.
"""

import shutil
import subprocess
from abc import ABC, abstractmethod

# Điểm xuất phát (tiền lệ group call), CHƯA xác minh cho gọi riêng tư (T1).
ENGINE_PCM_FORMAT = {"sample_rate": 48000, "channels": 1, "format": "s16le"}


class MicSource(ABC):
    """Nguồn mic: luồng async cho byte PCM + định dạng native."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def read_chunk(self) -> bytes: ...

    @property
    @abstractmethod
    def native_format(self) -> dict: ...


class SpeakerSink(ABC):
    """Đích loa: nhận byte PCM + định dạng native."""

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def write(self, pcm: bytes) -> None: ...

    @property
    @abstractmethod
    def native_format(self) -> dict: ...


class FileSource(MicSource):
    """Mic từ file PCM — kiểm thử (và ghi âm khi bật, mặc định tắt)."""

    def __init__(self, path: str, native_format: dict | None = None):
        self.path = path
        self._fh = None
        self._native = native_format or dict(ENGINE_PCM_FORMAT)

    @property
    def native_format(self) -> dict:
        return self._native

    def start(self) -> None:
        self._fh = open(self.path, "rb")

    def stop(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def read_chunk(self) -> bytes:
        if not self._fh:
            return b""
        return self._fh.read(9600)  # ~100 ms ở 48 kHz mono s16le


class FileSink(SpeakerSink):
    """Loa ghi ra file PCM — kiểm thử."""

    def __init__(self, path: str, native_format: dict | None = None):
        self.path = path
        self._fh = None
        self._native = native_format or dict(ENGINE_PCM_FORMAT)

    @property
    def native_format(self) -> dict:
        return self._native

    def start(self) -> None:
        self._fh = open(self.path, "wb")

    def stop(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def write(self, pcm: bytes) -> None:
        if self._fh:
            self._fh.write(pcm)


class PipeSource(MicSource):
    """Mic từ pipe trong tiến trình: test/endpoint tùy chỉnh đẩy PCM vào
    bằng ``feed()``; ``read_chunk`` lấy một khối, hết khối -> b"" (im
    lặng ở tầng FrameQueue, không chặn vô hạn)."""

    def __init__(self, native_format: dict | None = None):
        import queue
        self._queue: queue.Queue = queue.Queue()
        self._native = native_format or dict(ENGINE_PCM_FORMAT)
        self._running = False

    @property
    def native_format(self) -> dict:
        return self._native

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def feed(self, pcm: bytes) -> None:
        if pcm:
            self._queue.put(bytes(pcm))

    def read_chunk(self) -> bytes:
        import queue as _q
        try:
            return self._queue.get_nowait()
        except _q.Empty:
            return b""


class PipeSink(SpeakerSink):
    """Loa ra pipe trong tiến trình: ``write`` gom khối, ``drain()``
    lấy toàn bộ PCM đã nhận (kiểm thử / endpoint tùy chỉnh đọc)."""

    def __init__(self, native_format: dict | None = None):
        self._chunks: list[bytes] = []
        self._native = native_format or dict(ENGINE_PCM_FORMAT)
        self._running = False

    @property
    def native_format(self) -> dict:
        return self._native

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    def write(self, pcm: bytes) -> None:
        if pcm:
            self._chunks.append(bytes(pcm))

    def drain(self) -> bytes:
        data = b"".join(self._chunks)
        self._chunks.clear()
        return data

    @property
    def chunks(self) -> list[bytes]:
        return list(self._chunks)


class _HardwareNotAvailable(RuntimeError):
    pass


class R1StreamSource(MicSource):
    """Endpoint R1 dạng luồng mạng — CHỈ INTERFACE + TÀI LIỆU ở P2.

    R1 thật cần phần cứng/giao thức luồng riêng của thiết bị (chưa có
    ở đây); ``start()`` raise rõ ràng thay vì giả vờ chạy. Khi R1 hiện
    trong HA như thực thể media/voice thì nó đi qua cầu HA (audio/in),
    không cần class này.
    """

    @property
    def native_format(self) -> dict:
        return dict(ENGINE_PCM_FORMAT)

    def start(self) -> None:
        raise _HardwareNotAvailable(
            "R1StreamSource cần thiết bị R1 + giao thức luồng thật (P2: "
            "chỉ interface, chưa triển khai).")

    def stop(self) -> None:
        pass

    def read_chunk(self) -> bytes:
        raise _HardwareNotAvailable("R1StreamSource chưa triển khai.")


class R1StreamSink(SpeakerSink):
    """Đối tác loa của R1StreamSource — chỉ interface ở P2."""

    @property
    def native_format(self) -> dict:
        return dict(ENGINE_PCM_FORMAT)

    def start(self) -> None:
        raise _HardwareNotAvailable(
            "R1StreamSink cần thiết bị R1 + giao thức luồng thật (P2: "
            "chỉ interface, chưa triển khai).")

    def stop(self) -> None:
        pass

    def write(self, pcm: bytes) -> None:
        raise _HardwareNotAvailable("R1StreamSink chưa triển khai.")


class UsbMicSource(MicSource):
    """Mic USB gắn vào server — chỉ interface ở P2 (cần map /dev/snd
    lúc chạy compose + ALSA; xem docker-compose.yml, đang comment)."""

    @property
    def native_format(self) -> dict:
        return dict(ENGINE_PCM_FORMAT)

    def start(self) -> None:
        raise _HardwareNotAvailable(
            "UsbMicSource cần mic USB gắn vào server (P2: chỉ interface).")

    def stop(self) -> None:
        pass

    def read_chunk(self) -> bytes:
        raise _HardwareNotAvailable("UsbMicSource chưa triển khai.")


class UsbSpeakerSink(SpeakerSink):
    """Loa USB gắn vào server — chỉ interface ở P2."""

    @property
    def native_format(self) -> dict:
        return dict(ENGINE_PCM_FORMAT)

    def start(self) -> None:
        raise _HardwareNotAvailable(
            "UsbSpeakerSink cần loa USB gắn vào server (P2: chỉ interface).")

    def stop(self) -> None:
        pass

    def write(self, pcm: bytes) -> None:
        raise _HardwareNotAvailable("UsbSpeakerSink chưa triển khai.")


def resample_pcm(pcm: bytes, src_rate: int, dst_rate: int,
                 channels: int = 1) -> bytes:
    """Resample PCM s16le bằng ffmpeg (nhị phân hệ thống).

    Trả về nguyên bản nếu tần số bằng nhau. Lỗi ffmpeg → RuntimeError
    (bước gọi P1/P2 bọc thành 502 MEDIA_ENGINE_ERROR).
    """
    if src_rate == dst_rate:
        return pcm
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg không có trên hệ thống.")
    cmd = [ffmpeg, "-v", "error", "-f", "s16le", "-ar", str(src_rate),
           "-ac", str(channels), "-i", "pipe:0", "-f", "s16le",
           "-ar", str(dst_rate), "-ac", str(channels), "pipe:1"]
    proc = subprocess.run(cmd, input=pcm, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg resample thất bại.")
    return proc.stdout


class AudioHub:
    """Nối MicSource/SpeakerSink với engine qua PCM.

    Khung P0: giữ cặp endpoint cho một cuộc gọi + kiểm ENDPOINT_BUSY
    (một endpoint vật lý chỉ thuộc một cuộc tại một thời điểm).
    P2: thêm hàng đợi khung mic theo seq (FrameQueue) + bộ đệm phát
    TTS cho một cuộc gọi.
    """

    def __init__(self):
        self._claimed: dict[str, str] = {}  # endpoint -> call_id
        self._inbound: dict[str, FrameQueue] = {}
        self._playback: dict[str, bytes] = {}

    def is_free(self, endpoint: str) -> bool:
        return endpoint not in self._claimed

    def claim(self, endpoint: str, call_id: str) -> bool:
        owner = self._claimed.get(endpoint)
        if owner is not None and owner != call_id:
            return False
        self._claimed[endpoint] = call_id
        return True

    def release(self, endpoint: str, call_id: str) -> None:
        if self._claimed.get(endpoint) == call_id:
            del self._claimed[endpoint]
        self._inbound.pop(call_id, None)
        self._playback.pop(call_id, None)

    # -- mic vào (HA -> engine) qua FrameQueue ---------------------------
    def push_in(self, call_id: str, seq: int, pcm: bytes) -> str:
        """Đẩy một khối mic. Trả 'ok' | 'backpressure' (hàng đợi đầy:
        route trả 429, HA chậm lại) | 'stale' (khối quá cũ, bỏ)."""
        fq = self._inbound.setdefault(call_id, FrameQueue())
        return fq.push(seq, pcm)

    def pop_in(self, call_id: str) -> bytes:
        """Lấy khối mic tiếp theo cho engine; mất khối -> im lặng
        (bytes 0) + đếm, không dừng cuộc gọi."""
        fq = self._inbound.get(call_id)
        if fq is None:
            return b"\x00" * FrameQueue.CHUNK_BYTES
        return fq.pop()

    def inbound_stats(self, call_id: str) -> dict:
        fq = self._inbound.get(call_id)
        if fq is None:
            return {"received": 0, "missing_silence": 0,
                    "dropped_stale": 0, "backpressure": 0}
        return fq.stats()

    # -- phát TTS vào cuộc gọi (engine IN) --------------------------------
    def queue_playback(self, call_id: str, pcm: bytes) -> None:
        self._playback[call_id] = bytes(pcm)

    def read_playback(self, call_id: str, nbytes: int) -> bytes:
        buf = self._playback.get(call_id, b"")
        chunk, rest = buf[:nbytes], buf[nbytes:]
        self._playback[call_id] = rest
        return chunk

    def playback_remaining(self, call_id: str) -> int:
        return len(self._playback.get(call_id, b""))


# --- PCM framing ---------------------------------------------------------
# Khối khuyến nghị 200 ms-1 s (thiết kế §6.3). Mặc định FrameQueue dùng
# khối 200 ms ở định dạng engine (48 kHz mono s16le = 19.200 byte).
# Khối quá cũ (> 2 s chưa tiêu thụ) bị bỏ (đàm thoại ưu tiên tươi hơn đủ).
class FrameQueue:
    CHUNK_BYTES = 19200  # 200 ms @ 48 kHz mono s16le
    MAX_CHUNKS = 50      # ~10 s đệm tối đa; đầy -> backpressure
    STALE_AFTER_SEQ_GAP = 10  # ~2 s ở khối 200 ms

    def __init__(self, chunk_bytes: int = CHUNK_BYTES,
                 max_chunks: int = MAX_CHUNKS):
        self.chunk_bytes = chunk_bytes
        self.max_chunks = max_chunks
        self._buffer: dict[int, bytes] = {}
        self._next_seq: int | None = None
        self.received = 0
        self.missing_silence = 0
        self.dropped_stale = 0
        self.backpressure = 0

    def push(self, seq: int, pcm: bytes) -> str:
        if len(self._buffer) >= self.max_chunks:
            self.backpressure += 1
            return "backpressure"
        if self._next_seq is not None and seq < self._next_seq:
            # Khối quá cũ (đã tiêu thụ qua seq này) -> bỏ, đếm.
            if self._next_seq - seq > self.STALE_AFTER_SEQ_GAP:
                self.dropped_stale += 1
                return "stale"
            return "stale"
        if self._next_seq is None:
            self._next_seq = seq
        self._buffer[seq] = bytes(pcm)
        self.received += 1
        return "ok"

    def pop(self) -> bytes:
        if self._next_seq is None:
            self.missing_silence += 1
            return b"\x00" * self.chunk_bytes
        chunk = self._buffer.pop(self._next_seq, None)
        self._next_seq += 1
        if chunk is None:
            # Mất khối -> im lặng đúng một khối, đếm, không dừng.
            self.missing_silence += 1
            return b"\x00" * self.chunk_bytes
        return chunk

    def stats(self) -> dict:
        return {"received": self.received,
                "missing_silence": self.missing_silence,
                "dropped_stale": self.dropped_stale,
                "backpressure": self.backpressure}
