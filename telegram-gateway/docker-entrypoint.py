#!/usr/bin/env python3
"""Entrypoint: chạy dưới add-on Home Assistant thì đổi tuỳ chọn (/data/options.json) thành biến môi trường.

Không có options.json = chạy Docker thường: không đụng vào gì, dùng .env như cũ.
Tuỳ chọn add-on: api_id, api_hash (lấy ở my.telegram.org), gateway_token (bỏ trống = tự sinh,
giữ lại trong /data/gateway_token 0600 và in ra Nhật ký), ha_webhook_url.
Có đủ api_id + api_hash thì bật engine thật (GATEWAY_MOCK=0); thiếu thì để chế độ giả lập và nói rõ.
Chỉ dùng thư viện chuẩn.
"""
import json
import os
import secrets
import sys

OPTIONS_FILE = "/data/options.json"
TOKEN_NAME = "gateway_token"


def apply_addon_options(env=None, options_path=OPTIONS_FILE, data_dir=None):
    """Trả về dict {"token_moi": str|None, "mock": bool} khi chạy add-on, None khi không phải add-on."""
    env = os.environ if env is None else env
    try:
        with open(options_path, encoding="utf-8") as fh:
            opts = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(opts, dict):
        return None
    data_dir = data_dir or env.get("DATA_DIR", "/data")
    api_id = str(opts.get("api_id") or "").strip()
    api_hash = str(opts.get("api_hash") or "").strip()
    token = str(opts.get("gateway_token") or "").strip()
    generated = None
    if not token:
        path = os.path.join(data_dir, TOKEN_NAME)
        try:
            with open(path, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError:
            pass
        if not token:
            token = secrets.token_hex(32)
            try:
                os.makedirs(data_dir, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(token + "\n")
            except OSError as e:
                print("entrypoint: không lưu được %s: %s" % (path, e), file=sys.stderr)
        generated = token
    env.setdefault("GATEWAY_TOKEN", token)
    if api_id and api_hash:
        env.setdefault("TELEGRAM_API_ID", api_id)
        env.setdefault("TELEGRAM_API_HASH", api_hash)
        env.setdefault("GATEWAY_MOCK", "0")
    else:
        env.setdefault("GATEWAY_MOCK", "1")
    url = str(opts.get("ha_webhook_url") or "").strip()
    if url:
        env.setdefault("HA_WEBHOOK_URL", url)
    return {"token_moi": generated, "mock": env.get("GATEWAY_MOCK") != "0"}


def main():
    info = apply_addon_options()
    if info is not None:
        if info["token_moi"]:
            print("Add-on Telegram Gateway: token cho tích hợp Telegram Call = %s" % info["token_moi"],
                  flush=True)
        if info["mock"]:
            print("Add-on Telegram Gateway: CHƯA điền api_id/api_hash ở tab Cấu hình, gateway đang "
                  "chạy chế độ giả lập (chưa đăng nhập hay gọi được Telegram thật).", flush=True)
    cmd = sys.argv[1:] or ["python", "-m", "gateway.main"]
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
