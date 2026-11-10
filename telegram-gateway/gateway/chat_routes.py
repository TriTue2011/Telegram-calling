"""Đường REST cho giao diện trò chuyện + mở lại tài khoản đã đăng nhập khi khởi động.

Mọi đường cần ``Authorization: Bearer <GATEWAY_TOKEN>`` như phần còn lại. Tài khoản chọn bằng ``?account=<số>``
(bỏ trống = tài khoản đang online đầu tiên). Gọi TDLib là đồng bộ nên chạy trong luồng phụ (``asyncio.to_thread``).
"""

import asyncio
import logging
import os
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse

from .chat import ChatService, Feed
from .live_call import CallRuntime
from .errors import ApiError, bad_request, not_found
from .tdclient import TelegramError

logger = logging.getLogger("telegram_gateway")

MAX_UPLOAD_BYTES = 64 * 1024 * 1024
UPLOAD_KEEP_SECONDS = 24 * 3600
KINDS = ("photo", "video", "voice", "audio", "document")


def add_chat_routes(app: FastAPI, *, data_dir: str, auth, body_dict, telegram_to_api, clients: dict,
                    store, logins, tdlib_factory) -> None:
    feed = Feed()
    services: dict[str, ChatService] = {}
    app.state.feed = feed
    app.state.chat_services = services
    uploads = os.path.join(data_dir, "uploads")
    os.makedirs(uploads, exist_ok=True)

    runtime: dict = {"rt": None}

    def get_runtime():
        """NTgCalls chỉ nạp khi cần (lần đầu có tài khoản thật); lỗi nạp không làm hỏng phần nhắn tin."""
        if runtime["rt"] is None:
            runtime["rt"] = CallRuntime(on_event=lambda kind, c: feed.push(
                "call", event=kind, call=c.info()), data_dir=data_dir)
        return runtime["rt"]

    def attach(phone: str, client) -> ChatService:
        """Gắn dịch vụ trò chuyện + cuộc gọi vào client (nhận update theo thời gian thực kể từ lúc này)."""
        svc = services.get(phone)
        if svc is None or svc.client is not client:
            svc = ChatService(client, feed=feed, account=phone)
            services[phone] = svc

            def on_update(obj, _svc=svc, _phone=phone, _client=client):
                _svc.handle_update(obj)
                if obj.get("@type") in ("updateCall", "updateNewCallSignalingData"):
                    try:
                        get_runtime().on_tdlib_update(_phone, _client, obj)
                    except Exception:  # noqa: BLE001 - lỗi cuộc gọi không được làm sập luồng nhận tin
                        logger.exception("xử lý update cuộc gọi lỗi")
            client.on_update = on_update
        return svc

    logins.on_client = attach

    def service(account: str | None) -> ChatService:
        if account and account in clients:
            return attach(account, clients[account])
        if not account and clients:
            phone = next(iter(clients))
            return attach(phone, clients[phone])
        raise ApiError(503, "ACCOUNT_OFFLINE", "Chưa có tài khoản Telegram nào đang đăng nhập (hoặc gateway đang giả lập).")

    async def run(fn, *args, **kw):
        try:
            return await asyncio.to_thread(fn, *args, **kw)
        except TelegramError as exc:
            raise telegram_to_api(exc)
        except TimeoutError:
            raise ApiError(504, "TELEGRAM_TIMEOUT", "Telegram không trả lời kịp.")

    def acct(request: Request) -> str | None:
        return request.query_params.get("account") or None

    def int_arg(request: Request, name: str, default: int = 0) -> int:
        try:
            return int(request.query_params.get(name, default))
        except ValueError:
            raise bad_request(f"Tham số {name} phải là số.")

    # -- thông tin chung -------------------------------------------------
    @app.get("/api/me")
    async def me(request: Request):
        auth(request)
        return await run(service(acct(request)).me)

    @app.get("/api/chats")
    async def chats(request: Request):
        auth(request)
        return {"chats": await run(service(acct(request)).chats, max(1, min(200, int_arg(request, "limit", 60))))}

    @app.get("/api/chats/{chat_id}/messages")
    async def messages(request: Request, chat_id: int):
        auth(request)
        svc = service(acct(request))
        return {"messages": await run(svc.history, chat_id, int_arg(request, "before"),
                                      max(1, min(100, int_arg(request, "limit", 30))))}

    @app.post("/api/chats/{chat_id}/send")
    async def send(request: Request, chat_id: int):
        auth(request)
        body = await body_dict(request)
        text = str(body.get("text") or "")
        if not text.strip():
            raise bad_request("Thiếu nội dung tin.")
        if len(text) > 4096:
            raise bad_request("Tin quá dài (tối đa 4096 ký tự).")
        return await run(service(acct(request)).send_text, chat_id, text, int(body.get("reply_to") or 0))

    @app.post("/api/chats/{chat_id}/send_file")
    async def send_file(request: Request, chat_id: int):
        """Thân request là NỘI DUNG TỆP thô (không multipart). ?kind=&name=&caption="""
        auth(request)
        kind = request.query_params.get("kind", "document")
        if kind not in KINDS:
            raise bad_request("kind phải là photo, video, voice, audio hoặc document.")
        raw = await request.body()
        if not raw:
            raise bad_request("Tệp rỗng.")
        if len(raw) > MAX_UPLOAD_BYTES:
            raise bad_request("Tệp vượt 64 MB.")
        name = os.path.basename(request.query_params.get("name", "") or "tep") or "tep"
        path = os.path.join(uploads, uuid.uuid4().hex + "_" + name.replace("/", "_"))
        with open(path, "wb") as fh:
            fh.write(raw)
        _sweep_uploads()
        return await run(service(acct(request)).send_file, chat_id, path, kind,
                         request.query_params.get("caption", ""))

    def _sweep_uploads() -> None:
        """Tệp tải lên phải còn tới khi TDLib gửi xong; dọn tệp quá một ngày."""
        now = time.time()
        for n in os.listdir(uploads):
            p = os.path.join(uploads, n)
            try:
                if now - os.path.getmtime(p) > UPLOAD_KEEP_SECONDS:
                    os.remove(p)
            except OSError:
                pass

    @app.post("/api/chats/{chat_id}/read")
    async def read(request: Request, chat_id: int):
        auth(request)
        body = await body_dict(request)
        ids = [int(i) for i in (body.get("message_ids") or [])][:100]
        await run(service(acct(request)).mark_read, chat_id, ids)
        return {"ok": True}

    # -- thao tác trên tin -------------------------------------------------
    @app.post("/api/messages/edit")
    async def edit(request: Request):
        auth(request)
        b = await body_dict(request)
        if not str(b.get("text") or "").strip():
            raise bad_request("Thiếu nội dung mới.")
        return await run(service(acct(request)).edit_text, int(b["chat_id"]), int(b["message_id"]), str(b["text"]))

    @app.post("/api/messages/delete")
    async def delete(request: Request):
        auth(request)
        b = await body_dict(request)
        await run(service(acct(request)).delete, int(b["chat_id"]), [int(i) for i in b.get("message_ids") or []],
                  bool(b.get("revoke", True)))
        return {"ok": True}

    @app.post("/api/messages/forward")
    async def forward(request: Request):
        auth(request)
        b = await body_dict(request)
        await run(service(acct(request)).forward, int(b["to_chat_id"]), int(b["from_chat_id"]),
                  [int(i) for i in b.get("message_ids") or []])
        return {"ok": True}

    @app.post("/api/messages/react")
    async def react(request: Request):
        auth(request)
        b = await body_dict(request)
        await run(service(acct(request)).react, int(b["chat_id"]), int(b["message_id"]),
                  str(b.get("emoji") or "👍"), bool(b.get("on", True)))
        return {"ok": True}

    # -- danh bạ, tìm kiếm -------------------------------------------------
    @app.get("/api/contacts")
    async def contacts(request: Request):
        auth(request)
        return {"contacts": await run(service(acct(request)).contacts)}

    @app.post("/api/contacts/open")
    async def open_chat(request: Request):
        auth(request)
        b = await body_dict(request)
        return {"chat_id": await run(service(acct(request)).open_private, int(b["user_id"]))}

    @app.get("/api/search")
    async def search(request: Request):
        auth(request)
        q = request.query_params.get("q", "").strip()
        if not q:
            return {"chats": []}
        return {"chats": await run(service(acct(request)).search, q)}

    # -- tệp (ảnh, âm thanh, video, tệp): tải về gateway rồi trả nội dung ----
    @app.get("/api/file/{file_id}")
    async def file(request: Request, file_id: int):
        auth(request)
        path = await run(service(acct(request)).download, file_id)
        if not path or not os.path.exists(path):
            raise not_found("Tệp chưa tải xong.")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})

    # -- cập nhật theo thời gian thực (long-poll) ----------------------------
    @app.get("/api/feed")
    async def get_feed(request: Request):
        auth(request)
        wait = max(0.0, min(25.0, float(request.query_params.get("wait", 0) or 0)))
        return await asyncio.to_thread(feed.since, int_arg(request, "since"), wait)

    # -- cuộc gọi thật (thoại + video) ----------------------------------------------------------------
    settings_path = os.path.join(data_dir, "live_settings.json")
    DEFAULT_SETTINGS = {"mic": "test", "mic_format": "auto", "camera": "test", "playback": "file"}

    def load_settings() -> dict:
        import json
        out = dict(DEFAULT_SETTINGS)
        try:
            with open(settings_path, encoding="utf-8") as fh:
                saved = json.load(fh)
            out.update({k: str(v) for k, v in saved.items() if k in out})
        except (OSError, ValueError):
            pass
        return out

    def _mask(v: str) -> str:
        import re as _re
        return _re.sub(r"(://[^/\s:@]+:)[^@/\s]*@", r"\1***@", v or "")

    @app.get("/api/live/settings")
    async def get_live_settings(request: Request):
        auth(request)
        s = load_settings()
        return {"mic": _mask(s["mic"]), "mic_format": s["mic_format"], "camera": _mask(s["camera"]),
                "playback": s["playback"]}

    @app.post("/api/live/settings")
    async def set_live_settings(request: Request):
        """Nguồn mic/camera MẶC ĐỊNH cho cuộc gọi: "test" hoặc URL (rtsp/http) / tệp. Có mật khẩu: chỉ ghi, không trả lại."""
        import json
        auth(request)
        b = await body_dict(request)
        s = load_settings()
        for k in ("mic", "camera"):
            if k in b:
                v = str(b[k] or "test").strip()
                if v != "test" and not (v.startswith(("rtsp://", "rtsps://", "http://", "https://", "rtmp://")) or v.startswith("/")):
                    raise bad_request(f"{k} phải là test, URL rtsp/http hoặc đường dẫn tệp.")
                if "***@" not in v:
                    s[k] = v
        if b.get("mic_format") in ("auto", "s16le16k"):
            s["mic_format"] = b["mic_format"]
        if b.get("playback") in ("external", "file"):
            s["playback"] = b["playback"]
        tmp = settings_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(s, fh)
        os.replace(tmp, settings_path)
        return {"ok": True}

    def _phone_of(request: Request) -> str:
        svc = service(acct(request))
        return svc.account

    @app.post("/api/live/call")
    async def live_call(request: Request):
        auth(request)
        b = await body_dict(request)
        phone = _phone_of(request)
        try:
            user_id = int(b["user_id"])
        except (KeyError, ValueError, TypeError):
            raise bad_request("Thiếu user_id (số).")
        rt = get_runtime()
        try:
            d = load_settings()           # nguồn mặc định (có thể ghi đè từng cuộc bằng mic/camera trong body)
            call = await run(rt.call_out, phone, clients[phone], user_id, bool(b.get("video", False)),
                             str(b.get("mic") or d["mic"]), str(b.get("mic_format") or d["mic_format"]),
                             str(b.get("camera") or d["camera"]), str(b.get("playback") or d["playback"]))
        except RuntimeError as exc:
            raise ApiError(409, "CALL_BUSY", str(exc))
        return call.info()

    @app.get("/api/live/calls")
    async def live_calls(request: Request):
        auth(request)
        rt = runtime["rt"]
        return {"calls": [c.info() for c in (rt.calls.values() if rt else [])][-10:]}

    @app.post("/api/live/hangup")
    async def live_hangup(request: Request):
        auth(request)
        b = await body_dict(request)
        rt = runtime["rt"]
        c = rt.calls.get(str(b.get("id"))) if rt else None
        if c is None:
            raise not_found("Không có cuộc gọi này.")
        await run(c.hangup)
        return c.info()

    @app.post("/api/live/playback")
    async def live_playback(request: Request):
        """Đổi cách nhận tiếng bên kia ngay trong cuộc gọi: kind = external | file | shell (+ cmd)."""
        auth(request)
        b = await body_dict(request)
        rt = runtime["rt"]
        c = rt.calls.get(str(b.get("id"))) if rt else None
        if c is None:
            raise not_found("Không có cuộc gọi này.")
        kind = str(b.get("kind") or "file")
        if kind not in ("external", "file", "shell"):
            raise bad_request("kind phải là external, file hoặc shell.")
        asyncio.run_coroutine_threadsafe(c._set_playback(kind, str(b.get("cmd") or "")), rt.loop)
        await asyncio.sleep(0.5)
        return c.info()

    @app.post("/api/live/accept")
    async def live_accept(request: Request):
        auth(request)
        b = await body_dict(request)
        rt = runtime["rt"]
        c = rt.calls.get(str(b.get("id"))) if rt else None
        if c is None or c.outgoing:
            raise not_found("Không có cuộc gọi đến này.")
        await run(c.accept)
        return c.info()

    # -- mở lại tài khoản đã đăng nhập khi khởi động ------------------------
    def restore() -> None:
        if tdlib_factory is None:
            return
        for acc in list(store.list()):
            if acc.status == "mock":
                continue
            phone = acc.phone
            client = None
            try:
                client = tdlib_factory(phone, store.ensure_session_dir(phone))
                client.open()
                state = ""
                for _ in range(60):                     # DB cục bộ có thể mất vài giây để mở
                    state = client.authorization_state()
                    if state and state != "authorizationStateWaitTdlibParameters":
                        break
                    time.sleep(0.25)
                if state == "authorizationStateReady":
                    clients[phone] = client
                    attach(phone, client)
                    store.mark_online(phone)
                    logger.info("đã mở lại tài khoản online")
                else:
                    logger.warning("phiên đăng nhập hết hạn (state=%s): cần đăng nhập lại", state)
                    store.set_status(phone, "offline")
                    client.close()
            except Exception:  # noqa: BLE001 - một tài khoản hỏng không chặn các tài khoản khác
                logger.exception("không mở lại được tài khoản")
                store.set_status(phone, "offline")
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # noqa: BLE001
                        pass

    app.state.restore_accounts = restore
    if tdlib_factory is not None:
        threading.Thread(target=restore, name="restore-accounts", daemon=True).start()
