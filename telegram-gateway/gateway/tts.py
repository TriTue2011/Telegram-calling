"""TTS vào cuộc gọi (P2) + nạp audio TTS của HA theo khối.

Mặc định: Edge TTS (giọng ``vi-VN-HoaiMyNeural``, rate +0% — điểm xuất
phát mượn từ telegram_voip, chỉnh sau kiểm thử thật). Interface
``Synthesizer`` inject được: test dùng fake, production dùng
``EdgeTtsSynthesizer``. Edge TTS KHÔNG gọi mạng ở test (fake) và cũng
không được import ở module load — chỉ import khi synthesize thật, nếu
thiếu package thì raise RuntimeError rõ ràng (cần ``pip install
edge-tts`` trên image server; requirements.txt chưa thêm theo brief
giữ nguyên đóng gói — xem báo cáo P1).

TTS của HA (``tts.speak``): HA nạp file audio theo khối base64 qua
``POST /api/audio/upload`` (mỗi khối <= 300 KB, nhiều lần gọi với cùng
``upload_id``, lần cuối ``done: true`` -> ráp file, trả ``tts_audio_id``).
File chỉ dùng cho cuộc gọi đó: xóa sau khi cuộc kết thúc hoặc sau 1 giờ,
tùy cái nào trước (route hangup xóa; ``prune_expired`` dọn phần còn lại).
"""

import base64
import binascii
import os
import subprocess
import time
import uuid
from abc import ABC, abstractmethod

UPLOAD_DIR = "tts_uploads"
UPLOAD_TTL_SECONDS = 3600  # 1 giờ
MAX_CHUNK_BYTES = 300_000  # khối base64 tối đa theo thiết kế §6.4


class Synthesizer(ABC):
    @abstractmethod
    def synthesize(self, text: str, voice: str, rate: str) -> bytes:
        """Trả PCM s16le 48 kHz mono (định dạng engine; xem audio.py
        TODO T1 — định dạng gọi riêng tư vẫn chờ xác minh)."""


class EdgeTtsSynthesizer(Synthesizer):
    """Edge TTS thật qua package edge_tts (async) + ffmpeg decode ra PCM.

    Chạy đồng bộ bằng asyncio.run trong tiến trình gateway. Không có
    edge_tts -> RuntimeError rõ ràng (route bọc thành 502).
    """

    def __init__(self, voice: str = "vi-VN-HoaiMyNeural",
                 rate: str = "+0%"):
        self.voice = voice
        self.rate = rate

    def synthesize(self, text: str, voice: str = "", rate: str = "") -> bytes:
        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError(
                "edge-tts chưa cài trên image này (cần pip install "
                "edge-tts) — TTS mặc định chưa chạy được.") from exc
        import asyncio
        import tempfile
        voice = voice or self.voice
        rate = rate or self.rate
        communicate = edge_tts.Communicate(text, voice, rate=rate)

        async def _save(path: str) -> None:
            await communicate.save(path)

        with tempfile.TemporaryDirectory() as tmp:
            mp3_path = os.path.join(tmp, "tts.mp3")
            # Dịch vụ của Microsoft thỉnh thoảng trả «No audio was received» (đo thật: lần 1 hỏng, lần 2 được): thử lại tối đa 3 lần.
            for attempt in range(3):
                try:
                    asyncio.run(_save(mp3_path))
                    break
                except edge_tts.exceptions.NoAudioReceived:
                    if attempt == 2:
                        raise
                    communicate = edge_tts.Communicate(text, voice, rate=rate)
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", mp3_path, "-f", "s16le",
                 "-ar", "48000", "-ac", "1", "pipe:1"],
                capture_output=True, timeout=120)
            if proc.returncode != 0:
                raise RuntimeError("ffmpeg decode TTS thất bại.")
            return proc.stdout


class TtsUploadStore:
    """Ráp file TTS của HA từ các khối base64, lưu dưới data_dir."""

    def __init__(self, data_dir: str):
        self.dir = os.path.join(data_dir, UPLOAD_DIR)
        os.makedirs(self.dir, exist_ok=True)
        try:
            os.chmod(self.dir, 0o700)
        except OSError:
            pass
        self._parts: dict[str, list[bytes]] = {}
        self._created: dict[str, float] = {}

    def add_chunk(self, upload_id: str | None, chunk_b64: str,
                  done: bool = False) -> dict:
        """Trả {'upload_id': ...} khi chưa xong, hoặc
        {'tts_audio_id': ..., 'size': ...} khi done."""
        if upload_id is None:
            upload_id = uuid.uuid4().hex
            self._parts[upload_id] = []
            self._created[upload_id] = time.time()
        if upload_id not in self._parts:
            raise KeyError(f"upload_id không tồn tại: {upload_id}")
        if chunk_b64:
            try:
                raw = base64.b64decode(chunk_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("chunk_b64 không phải base64 hợp lệ.") from exc
            if len(chunk_b64.encode("ascii")) > MAX_CHUNK_BYTES:
                raise ValueError("Khối vượt 300 KB (chia nhỏ hơn).")
            self._parts[upload_id].append(raw)
        if not done:
            return {"upload_id": upload_id}
        data = b"".join(self._parts.pop(upload_id))
        self._created.pop(upload_id, None)
        tts_audio_id = uuid.uuid4().hex
        path = os.path.join(self.dir, tts_audio_id)
        with open(path, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return {"tts_audio_id": tts_audio_id, "size": len(data)}

    def get_path(self, tts_audio_id: str) -> str | None:
        path = os.path.join(self.dir, tts_audio_id)
        return path if os.path.exists(path) else None

    def delete(self, tts_audio_id: str) -> bool:
        path = os.path.join(self.dir, tts_audio_id)
        try:
            os.remove(path)
            return True
        except OSError:
            return False

    def prune_expired(self, ttl: int = UPLOAD_TTL_SECONDS) -> int:
        now = time.time()
        removed = 0
        try:
            for name in os.listdir(self.dir):
                path = os.path.join(self.dir, name)
                try:
                    if now - os.path.getmtime(path) > ttl:
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
        except OSError:
            pass
        return removed
