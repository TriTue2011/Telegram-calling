"""Đổi âm thanh bất kỳ (mp3, m4a, wav, aac...) sang OGG/Opus: Telegram chỉ nhận tin thoại ở định dạng này.

Chạy ffmpeg qua đường ống (không tệp tạm), có hạn giờ để một tệp lỗi không treo gateway."""
from __future__ import annotations

import subprocess

MAX_SECONDS = 600
TIMEOUT_S = 60
_FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-i", "pipe:0", "-vn", "-t", str(MAX_SECONDS),
           "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-f", "ogg", "pipe:1"]


def is_ogg(raw: bytes) -> bool:
    return raw[:4] == b"OggS"


def to_ogg_opus(raw: bytes, runner=subprocess.run) -> bytes:
    """Trả về OGG/Opus. Đã là Ogg thì giữ nguyên. Lỗi ffmpeg thì ValueError kèm dòng lỗi (không lộ dữ liệu)."""
    if is_ogg(raw):
        return raw
    try:
        p = runner(_FFMPEG, input=raw, capture_output=True, timeout=TIMEOUT_S)
    except FileNotFoundError:
        raise ValueError("máy chủ chưa có ffmpeg nên không đổi được âm thanh sang ogg/opus")
    except subprocess.TimeoutExpired:
        raise ValueError("đổi âm thanh quá %d giây" % TIMEOUT_S)
    if p.returncode != 0 or not p.stdout:
        err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["không rõ"]
        raise ValueError("ffmpeg không đọc được tệp âm thanh: " + err[0][:160])
    return p.stdout


_PCM_FFMPEG = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "s16le", "-ar", "48000", "-ac", "1", "-i", "pipe:0",
               "-c:a", "libopus", "-b:a", "32k", "-application", "voip", "-f", "ogg", "pipe:1"]


def pcm_to_ogg_opus(pcm: bytes, runner=subprocess.run) -> bytes:
    """PCM thô s16le 48 kHz mono (đầu ra của bộ tổng hợp giọng, dùng cho cuộc gọi) -> OGG/Opus để gửi làm tin thoại."""
    if not pcm:
        raise ValueError("bộ tổng hợp giọng không trả âm thanh")
    try:
        p = runner(_PCM_FFMPEG, input=pcm, capture_output=True, timeout=TIMEOUT_S)
    except FileNotFoundError:
        raise ValueError("máy chủ chưa có ffmpeg nên không đổi được âm thanh sang ogg/opus")
    except subprocess.TimeoutExpired:
        raise ValueError("đổi âm thanh quá %d giây" % TIMEOUT_S)
    if p.returncode != 0 or not p.stdout:
        err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["không rõ"]
        raise ValueError("ffmpeg không đổi được PCM sang ogg/opus: " + err[0][:160])
    return p.stdout
