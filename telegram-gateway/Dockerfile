FROM python:3.12-slim

# ffmpeg: decode TTS, resample PCM, mic/loa ALSA. glibc trên bookworm là
# 2.36 — đủ cho tdjson (manylinux_2_17) và ntgcalls (manylinux_2_28,
# cần glibc >= 2.28, điểm gating cho image).
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.source="https://github.com/TriTue2011/Telegram-calling" \
      org.opencontainers.image.description="Telegram gateway: TDLib + NTgCalls, REST + WebUI"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY gateway/ ./gateway/
COPY VERSION ./VERSION
COPY docker-entrypoint.py /usr/local/bin/docker-entrypoint.py

ENV DATA_DIR=/data
ENV REST_PORT=3333
EXPOSE 3333

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3333/health')"

ENTRYPOINT ["python", "/usr/local/bin/docker-entrypoint.py"]
CMD ["python", "-m", "gateway.main"]
