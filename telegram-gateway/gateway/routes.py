"""REST API của gateway — gốc http://<server>:3333, CHỈ LAN.

Auth: header Authorization: Bearer <GATEWAY_TOKEN>. Chỉ duy nhất
GET /health không cần auth (để compose healthcheck và watchdog gọi).
Đăng nhập KHÔNG nằm ở /api/* (bề mặt HA): login chỉ qua web UI nội bộ
(/ui/*) — đúng luật "login chỉ qua UI nội bộ" của thiết kế.

Bảng đường (ngữ nghĩa + mã lỗi theo thiết kế module, §4):
  GET  /health                       (không auth)
  GET  /api/accounts                chỉ đọc, không bí mật
  POST /api/send_message            thật nếu tài khoản đã gắn client
                                     TDLib, ngược lại mock (mock_mode)
                                     hoặc 503 khi engine thật mà offline
  POST /api/send_media              thật nếu có client, ngược lại 501 P4
  POST /api/send_voice              thật nếu có client, ngược lại 501 P4
  POST /api/call                    trả ngay {call_id, status: "dialing"}
  POST /api/answer_call             state machine thật
  POST /api/hangup_call             state machine thật + dọn TTS file
  GET  /api/calls
  GET  /api/chats, /api/chats/{id}/messages, /api/contacts, /api/search, /api/file/{id}, /api/feed ...
                                    xem chat_routes.py (giao diện trò chuyện)
  GET  /api/events?since=<cursor>   poll fallback
  POST /api/audio/in                mic HA -> engine (khối JSON, P2)
  POST /api/audio/upload            nạp audio TTS HA theo khối (P2)
  Đường lạ -> 404, sai phương thức -> 405, thân POST không phải dict -> 400.
"""

import asyncio
import base64
import binascii
import os
import tempfile
import time
from collections import defaultdict, deque

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.responses import JSONResponse

from .audio import AudioHub, resample_pcm
from .calls import CallManager
from .config import Config
from .errors import (ApiError, account_busy, account_offline, bad_request,
                     endpoint_busy, flood_wait, media_engine_error,
                     not_found, not_implemented_yet)
from .events import EventBridge
from .login import LoginManager
from .redact import mask_peer
from .store import AccountStore
from .tdclient import TelegramError
from .tts import TtsUploadStore

MAX_BODY_BYTES = 400_000


def error_body(exc: ApiError) -> dict:
    return exc.detail


def _telegram_to_api(exc: TelegramError) -> ApiError:
    if exc.flood_wait_seconds is not None:
        return flood_wait(exc.flood_wait_seconds)
    if 400 <= exc.code < 500:
        return ApiError(exc.code, "TELEGRAM_ERROR",
                        f"Telegram từ chối: {exc.message or exc.code}")
    return ApiError(502, "MEDIA_ENGINE_ERROR",
                    f"Lỗi phía Telegram: {exc.message or exc.code}")


def build_app(cfg: Config, store: AccountStore | None = None,
              calls: CallManager | None = None,
              events: EventBridge | None = None,
              audio: AudioHub | None = None,
              logins: LoginManager | None = None,
              clients: dict | None = None,
              tdlib_factory=None,
              synthesizer=None,
              tts_uploads: TtsUploadStore | None = None) -> FastAPI:
    store = store or AccountStore(cfg.data_dir)
    calls = calls or CallManager(cfg.queue_depth, cfg.queue_timeout)
    events = events or EventBridge(cfg.data_dir, cfg.ha_webhook_url,
                                   cfg.ha_webhook_token)
    audio = audio or AudioHub()
    if clients is None:
        clients = logins.clients if logins is not None else {}
    logins = logins or LoginManager(store, clients=clients,
                                    client_factory=tdlib_factory)
    tts_uploads = tts_uploads or TtsUploadStore(cfg.data_dir)
    started_at = time.time()

    app = FastAPI(title="telegram-gateway", version=cfg.version)
    app.state.store = store
    app.state.calls = calls
    app.state.events = events
    app.state.audio = audio
    app.state.logins = logins
    app.state.clients = clients
    app.state.synthesizer = synthesizer
    app.state.tts_uploads = tts_uploads

    # --- Rate limit: 120 request/phút/token (health không tính) ---
    _hits: dict[str, deque] = defaultdict(deque)

    def _rate_limited(token: str) -> bool:
        now = time.time()
        dq = _hits[token]
        while dq and now - dq[0] > 60:
            dq.popleft()
        if len(dq) >= cfg.rate_limit_per_minute:
            return True
        dq.append(now)
        return False

    def _auth(request: Request) -> None:
        header = request.headers.get("Authorization", "")
        token = header[7:] if header.startswith("Bearer ") else ""
        if token != cfg.gateway_token:
            raise ApiError(401, "UNAUTHORIZED", "Thiếu hoặc sai token.")
        if _rate_limited(token):
            raise ApiError(429, "RATE_LIMITED",
                           "Quá 120 request/phút cho token này.")

    async def _body_dict(request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            raise bad_request("Thân request vượt 400 KB.")
        if not raw:
            return {}
        try:
            import json
            data = json.loads(raw)
        except ValueError:
            raise bad_request("Thân request không phải JSON hợp lệ.")
        if not isinstance(data, dict):
            raise bad_request("Thân POST phải là JSON object.")
        return data

    def _account_or_400(body: dict) -> str:
        account = body.get("account") or ""
        if not account:
            online = [a for a in store.list() if a.status in ("online", "mock")]
            if not online:
                raise account_offline()
            return online[0].phone
        acc = store.get(account)
        if acc is None:
            raise not_found(f"Không rõ tài khoản: {mask_peer(account)}")
        if acc.status == "offline":
            raise account_offline()
        return account

    def _bound_client(body: dict):
        """Client TDLib đã gắn cho tài khoản yêu cầu (None nếu chưa có)."""
        account = body.get("account") or ""
        if account and account in clients:
            return account, clients[account]
        if not account and clients:
            first = next(iter(clients))
            return first, clients[first]
        return account or None, None

    def _decode_to_pcm(path: str) -> bytes:
        """Decode file audio về PCM engine; ffmpeg lỗi -> dùng raw."""
        try:
            import subprocess
            proc = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le",
                 "-ar", "48000", "-ac", "1", "pipe:1"],
                capture_output=True, timeout=60)
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except Exception:
            pass
        with open(path, "rb") as fh:
            return fh.read()

    @app.exception_handler(ApiError)
    async def _api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code,
                            content=error_body(exc))

    @app.get("/health")
    async def health():
        accounts = store.list()
        online = sum(1 for a in accounts if a.status in ("online", "mock"))
        return {"status": "ok" if online else "degraded",
                "version": cfg.version,
                "uptime_seconds": int(time.time() - started_at),
                "accounts_total": len(accounts),
                "accounts_online": online,
                "mock_mode": cfg.mock_mode}

    @app.get("/", include_in_schema=False)
    async def root():
        # Mở thẳng địa chỉ gateway (không có /ui/) thì đưa tới trang quản lý.
        return RedirectResponse("/ui/app.html")

    @app.get("/api/accounts")
    async def accounts(request: Request):
        _auth(request)
        return {"tai_khoan": [
            {"phone": a.phone, "display_name": a.display_name,
             "status": a.status} for a in store.list()]}

    @app.post("/api/send_message")
    async def send_message(request: Request):
        _auth(request)
        body = await _body_dict(request)
        peer = body.get("peer") or ""
        message = body.get("message") or ""
        if not peer or not message:
            raise bad_request("Thiếu peer hoặc message.")
        account = _account_or_400(body)
        client = clients.get(account)
        if client is not None:
            # P1: gửi thật qua MTProto (client TDLib).
            try:
                tin_id = client.send_message(peer, message)
            except TelegramError as exc:
                raise _telegram_to_api(exc)
            events.emit("message_new", account,
                        {"peer": mask_peer(peer), "tin_id": str(tin_id)})
            return {"ok": True, "tin_id": str(tin_id), "account": account,
                    "peer": peer, "mock": False}
        if not cfg.mock_mode:
            raise account_offline()
        # Mock (khung, khi chưa bật engine thật): ghi event, chưa gửi.
        tin_id = f"mock-{int(time.time() * 1000)}"
        events.emit("message_new", account,
                    {"peer": mask_peer(peer), "tin_id": tin_id,
                     "mock": True})
        return {"ok": True, "tin_id": tin_id, "account": account,
                "peer": peer, "mock": True}

    @app.post("/api/send_media")
    async def send_media(request: Request):
        _auth(request)
        body = await _body_dict(request)
        account, client = _bound_client(body)
        if client is None:
            raise not_implemented_yet("P4")
        media_b64 = body.get("media_b64") or ""
        peer = body.get("peer") or ""
        if not peer or not media_b64:
            raise bad_request("Thiếu peer hoặc media_b64.")
        try:
            raw = base64.b64decode(media_b64, validate=True)
        except (binascii.Error, ValueError):
            raise bad_request("media_b64 không phải base64 hợp lệ.")
        suffix = os.path.splitext(body.get("filename", "media.bin"))[1] or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(raw)
            tmp_path = tf.name
        try:
            tin_id = await asyncio.to_thread(client.send_media, peer, tmp_path,
                                             body.get("caption", ""))
        except TelegramError as exc:
            raise _telegram_to_api(exc)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return {"ok": True, "tin_id": str(tin_id), "account": account,
                "peer": peer, "mock": False}

    @app.post("/api/send_voice")
    async def send_voice(request: Request):
        _auth(request)
        body = await _body_dict(request)
        account, client = _bound_client(body)
        if client is None:
            raise not_implemented_yet("P4")
        peer = body.get("peer") or ""
        if not peer:
            raise bad_request("Thiếu peer.")
        tmp_path = None
        try:
            if body.get("audio_b64"):
                try:
                    raw = base64.b64decode(body["audio_b64"], validate=True)
                except (binascii.Error, ValueError):
                    raise bad_request("audio_b64 không phải base64 hợp lệ.")
                with tempfile.NamedTemporaryFile(suffix=".ogg",
                                                 delete=False) as tf:
                    tf.write(raw)
                    tmp_path = tf.name
            elif body.get("tts_text") and synthesizer is not None:
                # Bộ tổng hợp chạy đồng bộ (edge-tts dùng asyncio.run): phải ở luồng riêng, gọi thẳng trong vòng lặp
                # của FastAPI thì «asyncio.run() cannot be called from a running event loop» (gặp thật).
                pcm = await asyncio.to_thread(synthesizer.synthesize, body["tts_text"],
                                              body.get("tts_voice", ""), body.get("tts_rate", ""))
                with tempfile.NamedTemporaryFile(suffix=".raw",
                                                 delete=False) as tf:
                    tf.write(pcm)
                    tmp_path = tf.name
            else:
                raise bad_request("Thiếu audio_b64 hoặc tts_text.")
            tin_id = await asyncio.to_thread(client.send_voice, peer, tmp_path)
        except TelegramError as exc:
            raise _telegram_to_api(exc)
        except RuntimeError as exc:
            # Lỗi synthesizer (vd: edge-tts chưa cài) -> 502.
            raise media_engine_error(str(exc))
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        return {"ok": True, "tin_id": str(tin_id), "account": account,
                "peer": peer, "mock": False}

    @app.post("/api/call")
    async def call(request: Request):
        _auth(request)
        body = await _body_dict(request)
        peer = body.get("peer") or ""
        if not peer:
            raise bad_request("Thiếu peer.")
        account = _account_or_400(body)
        mode = body.get("mode", "tts")
        endpoint = body.get("audio_endpoint", "ha")
        # Thứ tự kiểm tra: tài khoản bận trước (409 ACCOUNT_BUSY),
        # rồi mới tới endpoint bận (409 ENDPOINT_BUSY). Cuộc trong hàng
        # đợi chưa claim endpoint — claim lúc được đưa lên (hangup).
        if calls.active_for(account) is not None and not body.get("queue", False):
            raise account_busy()
        if calls.active_for(account) is None and not audio.is_free(endpoint):
            raise endpoint_busy()
        tts_audio_id = body.get("tts_audio_id", "") or ""
        if mode == "tts" and tts_audio_id:
            if tts_uploads.get_path(tts_audio_id) is None:
                raise not_found("tts_audio_id không tồn tại.")
        c = calls.start_call(account, peer, mode,
                             tts_text=body.get("tts_text", ""),
                             queue=bool(body.get("queue", False)),
                             priority=body.get("priority", "normal"),
                             endpoint=endpoint,
                             tts_audio_id=tts_audio_id)
        if not c.queued:
            audio.claim(endpoint, c.call_id)
            # P2 TTS vào cuộc gọi: tổng hợp hoặc nạp file -> bộ đệm phát.
            try:
                if mode == "tts" and tts_audio_id:
                    path = tts_uploads.get_path(tts_audio_id)
                    audio.queue_playback(c.call_id, _decode_to_pcm(path))
                elif mode == "tts" and body.get("tts_text") and synthesizer is not None:
                    pcm = synthesizer.synthesize(
                        body["tts_text"], body.get("tts_voice", ""),
                        body.get("tts_rate", ""))
                    audio.queue_playback(c.call_id, pcm)
            except RuntimeError as exc:
                calls.hangup(c.call_id)
                audio.release(endpoint, c.call_id)
                raise media_engine_error(str(exc))
        is_mock = account not in clients
        events.emit("call_started", account,
                    {"call_id": c.call_id, "peer": mask_peer(peer),
                     "mode": mode, "mock": is_mock})
        # Trả NGAY dialing (bất đồng bộ, không long-poll).
        return {"call_id": c.call_id, "status": "dialing",
                "queued": c.queued, "mock": is_mock}

    @app.post("/api/answer_call")
    async def answer_call(request: Request):
        _auth(request)
        body = await _body_dict(request)
        call_id = body.get("call_id") or ""
        if not call_id:
            raise bad_request("Thiếu call_id.")
        c = calls.answer(call_id, mode=body.get("mode"))
        return {"ok": True, "call_id": c.call_id, "state": c.state}

    @app.post("/api/hangup_call")
    async def hangup_call(request: Request):
        _auth(request)
        body = await _body_dict(request)
        call_id = body.get("call_id") or ""
        if not call_id:
            raise bad_request("Thiếu call_id.")
        c = calls.hangup(call_id)
        audio.release(c.endpoint, c.call_id)
        if c.tts_audio_id:
            # File TTS của HA chỉ dùng cho cuộc này: xóa sau cuộc gọi.
            tts_uploads.delete(c.tts_audio_id)
        promoted = calls.last_promoted
        if promoted is not None and audio.is_free(promoted.endpoint):
            audio.claim(promoted.endpoint, promoted.call_id)
        events.emit("call_ended", c.account,
                    {"call_id": c.call_id, "reason": c.reason,
                     "duration_seconds": round(c.duration_seconds, 1)})
        return {"ok": True, "call_id": c.call_id, "state": c.state}

    @app.get("/api/calls")
    async def list_calls(request: Request):
        _auth(request)
        return {"calls": [{
            "call_id": c.call_id, "account": c.account, "peer": c.peer,
            "direction": c.direction, "mode": c.mode, "state": c.state,
            "started_at": c.started_at,
            "duration_seconds": round(c.duration_seconds, 1),
        } for c in calls.list_calls()]}

    @app.get("/api/events")
    async def poll_events(request: Request, since: int = 0):
        _auth(request)
        return events.poll(since)

    # --- Audio P2: mic HA lên + nạp TTS HA ------------------------------
    @app.post("/api/audio/in")
    async def audio_in(request: Request):
        _auth(request)
        body = await _body_dict(request)
        call_id = body.get("call_id") or ""
        pcm_b64 = body.get("pcm_b64") or ""
        if not call_id or not pcm_b64:
            raise bad_request("Thiếu call_id hoặc pcm_b64.")
        call = calls.get(call_id)
        if call is None or call.state == "ended":
            from .errors import call_not_active
            raise call_not_active()
        try:
            seq = int(body.get("seq", 0))
            pcm = base64.b64decode(pcm_b64, validate=True)
        except (ValueError, binascii.Error):
            raise bad_request("seq hoặc pcm_b64 không hợp lệ.")
        sample_rate = int(body.get("sample_rate", 48000) or 48000)
        if sample_rate != 48000:
            try:
                pcm = resample_pcm(pcm, sample_rate, 48000,
                                   int(body.get("channels", 1) or 1))
            except RuntimeError as exc:
                raise media_engine_error(str(exc))
        result = audio.push_in(call_id, seq, pcm)
        if result == "backpressure":
            raise ApiError(429, "BACKPRESSURE",
                           "Gateway tiêu thụ chậm hơn HA đẩy; HA chậm lại.")
        return {"ok": True, "call_id": call_id, "status": result}

    @app.post("/api/audio/upload")
    async def audio_upload(request: Request):
        _auth(request)
        body = await _body_dict(request)
        try:
            out = tts_uploads.add_chunk(body.get("upload_id"),
                                        body.get("chunk_b64", ""),
                                        bool(body.get("done", False)))
        except KeyError:
            raise not_found("upload_id không tồn tại.")
        except ValueError as exc:
            raise bad_request(str(exc))
        return out

    # --- Web UI nội bộ: đăng nhập (KHÔNG thuộc bề mặt HA) ---
    @app.post("/ui/api/accounts")
    async def ui_start_login(request: Request):
        _auth(request)
        body = await _body_dict(request)
        login_id = logins.start(body.get("phone", ""))
        return {"login_id": login_id}

    @app.post("/ui/api/accounts/qr")
    async def ui_start_qr(request: Request):
        _auth(request)
        body = await _body_dict(request)
        login_id, qr_link = logins.start_qr(body.get("phone", ""))
        return {"login_id": login_id, "qr_link": qr_link}

    @app.get("/ui/api/accounts/{login_id}")
    async def ui_login_status(request: Request, login_id: str):
        _auth(request)
        return logins.status(login_id)

    @app.post("/ui/api/accounts/{login_id}/code")
    async def ui_submit_code(request: Request, login_id: str):
        _auth(request)
        body = await _body_dict(request)
        result = logins.submit_code(login_id, body.get("code", ""))
        if isinstance(result, dict):
            return {"ok": True, "needs_password": True,
                    "stage": result.get("stage", "password")}
        return {"ok": True, "phone": result.phone, "status": result.status,
                "mock": result.status == "mock"}

    @app.post("/ui/api/accounts/{login_id}/password")
    async def ui_submit_password(request: Request, login_id: str):
        _auth(request)
        body = await _body_dict(request)
        acc = logins.submit_password(login_id, body.get("password", ""))
        return {"ok": True, "phone": acc.phone, "status": acc.status,
                "mock": acc.status == "mock"}

    @app.post("/ui/api/accounts/{login_id}/qr-confirm")
    async def ui_qr_confirm(request: Request, login_id: str):
        _auth(request)
        acc = logins.confirm_qr(login_id)
        return {"ok": True, "phone": acc.phone, "status": acc.status,
                "mock": acc.status == "mock"}

    @app.delete("/ui/api/accounts/{phone}")
    async def ui_delete_account(request: Request, phone: str):
        _auth(request)
        client = clients.pop(phone, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if not store.remove(phone):
            raise not_found("Không rõ tài khoản.")
        return {"ok": True}

    from .chat_routes import add_chat_routes
    add_chat_routes(app, data_dir=cfg.data_dir, auth=_auth, body_dict=_body_dict,
                    telegram_to_api=_telegram_to_api, clients=clients, store=store,
                    logins=logins, tdlib_factory=tdlib_factory)
    return app
