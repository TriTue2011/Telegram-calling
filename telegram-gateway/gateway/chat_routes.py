"""Đường REST cho giao diện trò chuyện + mở lại tài khoản đã đăng nhập khi khởi động.

Mọi đường cần ``Authorization: Bearer <GATEWAY_TOKEN>`` như phần còn lại. Tài khoản chọn bằng ``?account=<số>``
(bỏ trống = tài khoản đang online đầu tiên). Gọi TDLib là đồng bộ nên chạy trong luồng phụ (``asyncio.to_thread``).
"""

import asyncio
import base64
import logging
import os
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, Response, StreamingResponse

from . import td_services
from .chat import ChatService, Feed
from .media_convert import to_ogg_opus, to_pcm48k
from .live_call import CallRuntime, apply_defaults, sweep_stale_audio, valid_speaker_url
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
    sweep_stale_audio(data_dir)       # tệp tiếng bên kia của lần chạy trước (CallRuntime chỉ tạo ở cuộc gọi đầu tiên)

    runtime: dict = {"rt": None}
    app.state.live = runtime                      # kiểm thử thay runtime bằng bản giả

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
        return await run(service(acct(request)).send_text, chat_id, text, int(body.get("reply_to") or 0),
                         str(body.get("parse_mode") or ""))

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
        if kind == "voice":
            # Telegram chỉ nhận tin thoại là OGG/Opus: đổi mp3/m4a/wav... (ffmpeg, ngoài vòng lặp sự kiện).
            try:
                raw = await asyncio.to_thread(to_ogg_opus, raw)
            except ValueError as exc:
                raise bad_request(str(exc))
        name = os.path.basename(request.query_params.get("name", "") or "tep") or "tep"
        path = os.path.join(uploads, uuid.uuid4().hex + "_" + name.replace("/", "_"))
        with open(path, "wb") as fh:
            fh.write(raw)
        _sweep_uploads()
        return await run(service(acct(request)).send_file, chat_id, path, kind,
                         request.query_params.get("caption", ""), int_arg(request, "reply_to"),
                         request.query_params.get("parse_mode", ""))

    @app.post("/api/chats/{chat_id}/send_album")
    async def send_album(request: Request, chat_id: int):
        """Album thật: JSON {"items": [{"kind", "name", "data_b64", "caption"}], "reply_to", "parse_mode"} — 2-10 tệp, tổng tối đa 64 MB."""
        auth(request)
        body = await body_dict(request)
        raw_items = body.get("items")
        if not isinstance(raw_items, list) or not 2 <= len(raw_items) <= 10:
            raise bad_request("items phải là danh sách 2-10 tệp.")
        items, total = [], 0
        for it in raw_items:
            if not isinstance(it, dict):
                raise bad_request("Mỗi phần tử của items phải là một đối tượng.")
            kind = str(it.get("kind") or "document")
            if kind not in ("photo", "video", "document", "audio"):
                raise bad_request("kind trong album phải là photo, video, document hoặc audio.")
            try:
                data = base64.b64decode(str(it.get("data_b64") or ""), validate=True)
            except (ValueError, TypeError):
                raise bad_request("data_b64 không phải base64 hợp lệ.")
            if not data:
                raise bad_request("Có tệp rỗng trong album.")
            total += len(data)
            if total > MAX_UPLOAD_BYTES:
                raise bad_request("Album vượt 64 MB.")
            name = os.path.basename(str(it.get("name") or "") or "tep") or "tep"
            path = os.path.join(uploads, uuid.uuid4().hex + "_" + name.replace("/", "_"))
            with open(path, "wb") as fh:
                fh.write(data)
            items.append({"path": path, "kind": kind, "caption": str(it.get("caption") or "")})
        _sweep_uploads()
        try:
            msgs = await run(service(acct(request)).send_album, chat_id, items, int(body.get("reply_to") or 0), str(body.get("parse_mode") or ""))
        except ValueError as exc:
            raise bad_request(str(exc))
        return {"messages": msgs, "count": len(msgs)}

    @app.post("/api/do/{name}")
    async def do_service(request: Request, name: str):
        """Dịch vụ theo danh mục (td_services.HANDLERS): hội thoại, tin nhắn, danh bạ, nhóm, tài khoản. Chỉ tên trong danh sách cho phép; thân là JSON tham số của dịch vụ."""
        auth(request)
        if name not in td_services.HANDLERS:
            raise not_found("Không có dịch vụ «%s»." % name)
        body = await body_dict(request)
        try:
            return await run(td_services.run_service, service(acct(request)).client, name, body)
        except ValueError as exc:
            raise bad_request(str(exc))

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
        return await run(service(acct(request)).edit_text, int(b["chat_id"]), int(b["message_id"]), str(b["text"]),
                         str(b.get("parse_mode") or ""))

    @app.post("/api/resolve")
    async def resolve(request: Request):
        """Người nhận -> chat_id: ID số, ``@username``, hoặc ``+số điện thoại``. Số âm là chat_id sẵn của nhóm/kênh."""
        auth(request)
        b = await body_dict(request)
        peer = str(b.get("peer") or "").strip()
        if not peer:
            raise bad_request("Thiếu peer (@username, +số điện thoại hoặc ID số).")
        try:
            return {"chat_id": await run(clients[_phone_of(request)].resolve_chat, peer)}
        except ValueError as exc:
            raise bad_request(str(exc))

    @app.post("/api/translate")
    async def translate(request: Request):
        auth(request)
        b = await body_dict(request)
        try:
            return {"text": await run(service(acct(request)).translate, str(b.get("text") or ""), str(b.get("to_language_code") or ""))}
        except ValueError as exc:
            raise bad_request(str(exc))

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
    DEFAULT_SETTINGS = {"mic": "test", "mic_format": "auto", "camera": "test", "playback": "file", "speaker": ""}

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
        v = _re.sub(r"(://[^/\s:@]+:)[^@/\s]*@", r"\1***@", v or "")
        return _re.sub(r"([?&]k=)[^&\s]+", r"\1***", v)               # khoá ?k= của cửa bộ đàm / mic R1

    @app.get("/api/live/settings")
    async def get_live_settings(request: Request):
        auth(request)
        s = load_settings()
        return {"mic": _mask(s["mic"]), "mic_format": s["mic_format"], "camera": _mask(s["camera"]),
                "playback": s["playback"], "speaker": _mask(s["speaker"])}

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
                if v not in (("test", "silence") if k == "mic" else ("test", "black")) and not (v.startswith(("rtsp://", "rtsps://", "http://", "https://", "rtmp://")) or v.startswith("/")):
                    raise bad_request(f"{k} phải là {'test, silence' if k == 'mic' else 'test, black'}, URL rtsp/http hoặc đường dẫn tệp.")
                if "***@" not in v and "k=***" not in v:
                    s[k] = v
        if "speaker" in b:
            v = str(b["speaker"] or "").strip()
            if v and not valid_speaker_url(v) and "k=***" not in v:
                raise bad_request("speaker phải rỗng hoặc là địa chỉ cửa bộ đàm /api/r1_speaker|dahua_talk/intercom|duplex/<id>?k=<khoá>.")
            if "k=***" not in v:
                s["speaker"] = v
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
        if b.get("peer"):
            try:
                user_id = await run(clients[phone].resolve_user_id, b["peer"])
            except ValueError as exc:
                raise bad_request(str(exc))
        else:
            try:
                user_id = int(b["user_id"])
            except (KeyError, ValueError, TypeError):
                raise bad_request("Thiếu người nhận: user_id (số) hoặc peer (@username, +số điện thoại).")
        rt = get_runtime()
        try:
            d = load_settings()           # nguồn mặc định (có thể ghi đè từng cuộc bằng mic/camera/speaker trong body)
            speaker = str(b["speaker"] if "speaker" in b else d["speaker"] or "").strip()      # «speaker» có mặt mà rỗng = chủ động KHÔNG nối loa nào (cảnh báo)
            if speaker and not valid_speaker_url(speaker):
                raise bad_request("speaker phải là địa chỉ cửa bộ đàm /api/r1_speaker|dahua_talk/intercom|duplex/<id>?k=<khoá>.")
            call = await run(rt.call_out, phone, clients[phone], user_id, bool(b.get("video", False)),
                             str(b.get("mic") or d["mic"]), str(b.get("mic_format") or d["mic_format"]),
                             str(b.get("camera") or d["camera"]), str(b.get("playback") or d["playback"]), speaker)
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
        """Đổi cách nhận tiếng bên kia ngay trong cuộc gọi: kind = external | file | shell (+ cmd) | push (+ url cửa bộ đàm)."""
        auth(request)
        b = await body_dict(request)
        rt = runtime["rt"]
        c = rt.calls.get(str(b.get("id"))) if rt else None
        if c is None:
            raise not_found("Không có cuộc gọi này.")
        kind = str(b.get("kind") or "file")
        if kind not in ("external", "file", "shell", "push"):
            raise bad_request("kind phải là external, file, shell hoặc push.")
        if kind == "push" and not valid_speaker_url(str(b.get("url") or c.speaker_url)):
            raise bad_request("push cần url là địa chỉ cửa bộ đàm /api/r1_speaker|dahua_talk/intercom|duplex/<id>?k=<khoá>.")
        asyncio.run_coroutine_threadsafe(c._set_playback(kind, str(b.get("cmd") or b.get("url") or "")), rt.loop)
        await asyncio.sleep(0.5)
        return c.info()

    @app.post("/api/live/media")
    async def live_media(request: Request):
        """Đổi nguồn GỬI ĐI ngay trong cuộc gọi: ``mic`` ("silence" = tắt mic, "test", hoặc URL/tệp; kèm ``mic_format``), ``camera`` (nguồn hình),
        ``camera_off`` (true = tắt hình). Dùng cho «người nhà nói trực tiếp», tắt mic, tắt hình, đọc lời nhắn vào cuộc gọi."""
        auth(request)
        b = await body_dict(request)
        rt = runtime["rt"]
        c = rt.calls.get(str(b.get("id"))) if rt else None
        if c is None or c.state == "ended":
            raise not_found("Không có cuộc gọi đang diễn ra này.")
        kw = {}
        for k, arg in (("mic", "mic"), ("camera", "cam")):
            if k in b:
                v = str(b[k] or "test").strip()
                if v not in (("test", "silence") if k == "mic" else ("test", "black")) and not (v.startswith(("rtsp://", "rtsps://", "http://", "https://", "rtmp://")) or v.startswith("/")):
                    raise bad_request(f"{k} phải là {'test, silence' if k == 'mic' else 'test, black'}, URL rtsp/http hoặc đường dẫn tệp.")
                kw[arg] = v
        if b.get("mic_format") in ("auto", "s16le16k"):
            kw["mic_fmt"] = b["mic_format"]
        if "camera_off" in b:
            kw["cam_off"] = bool(b["camera_off"])
        if b.get("video"):
            kw["video"] = True                       # bật video giữa cuộc gọi thoại
        if not kw:
            raise bad_request("Cần mic, camera, camera_off hoặc video.")
        fut = asyncio.run_coroutine_threadsafe(c.set_capture(**kw), rt.loop)
        try:
            await asyncio.wait_for(asyncio.wrap_future(fut), 10)
        except Exception as exc:  # noqa: BLE001 - lỗi NTgCalls báo lại rõ, không làm sập route
            raise ApiError(502, "MEDIA_FAILED", "Không đổi được nguồn: %s %s" % (type(exc).__name__, str(exc)[:120]))
        return c.info()

    def _view_call(call_id: str):
        rt = runtime["rt"]
        c = rt.calls.get(call_id) if rt else None
        if c is None or c.state == "ended":
            raise not_found("Không có cuộc gọi đang diễn ra này.")
        return c

    @app.get("/api/live/view/{call_id}")
    async def live_view(call_id: str, request: Request):
        """Hình VIDEO của bên kia dạng MJPEG (multipart/x-mixed-replace) cho màn hình Dosen / trình duyệt. Chờ hình đầu tiên tối đa 8 giây; không có hình
        (cuộc gọi thoại, bên kia tắt camera) thì 409 — nơi nhận coi là «hết hình». Cần token như mọi route khác (``Authorization: Bearer``)."""
        auth(request)
        c = _view_call(call_id)
        for _ in range(80):
            if c.view.seq:
                break
            if c.state == "ended":
                raise not_found("Cuộc gọi đã kết thúc.")
            await asyncio.sleep(0.1)
        else:
            raise ApiError(409, "NO_VIDEO", "Chưa có hình video của bên kia.")

        async def gen():
            last, idle = 0, 0
            while c.state != "ended" and not await request.is_disconnected():
                if c.view.seq != last:
                    last, idle, jpg = c.view.seq, 0, c.view.jpeg
                    yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n"
                else:
                    idle += 1
                    if idle > 300:                         # 30 giây không có hình mới: bên kia đã tắt camera
                        break
                await asyncio.sleep(0.1)
        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"})

    @app.get("/api/live/snapshot/{call_id}")
    async def live_snapshot(call_id: str, request: Request):
        """Ảnh JPEG mới nhất của hình video bên kia (chẩn đoán / ảnh chụp)."""
        auth(request)
        c = _view_call(call_id)
        if not c.view.jpeg:
            raise ApiError(409, "NO_VIDEO", "Chưa có hình video của bên kia.")
        return Response(content=c.view.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/api/live/say")
    async def live_say(request: Request):
        """Đọc lời VÀO cuộc gọi đang nói chuyện: ``audio_b64`` (tệp âm thanh bất kỳ, ví dụ giọng TTS của Home Assistant) HOẶC ``text`` (bộ TTS của gateway).
        Trả ngay {seconds}: đoạn tiếng chạy nền, mic tự trả về như cũ khi đọc xong."""
        import base64
        import binascii
        auth(request)
        b = await body_dict(request)
        rt = runtime["rt"]
        c = rt.calls.get(str(b.get("id"))) if rt else None
        text = str(b.get("text") or "").strip()
        if not b.get("audio_b64") and (not text or len(text) > 1000):
            raise bad_request("Cần audio_b64 hoặc text (1-1000 ký tự).")
        if c is None or c.state != "active":
            raise not_found("Không có cuộc gọi đang nói chuyện này.")
        if b.get("audio_b64"):
            try:
                raw = base64.b64decode(b["audio_b64"], validate=True)
            except (binascii.Error, ValueError):
                raise bad_request("audio_b64 không phải base64 hợp lệ.")
            if len(raw) > MAX_UPLOAD_BYTES:
                raise bad_request("Âm thanh quá lớn.")
            try:
                pcm = await asyncio.to_thread(to_pcm48k, raw)
            except ValueError as exc:
                raise bad_request(str(exc))
        else:
            synth = getattr(app.state, "synthesizer", None)
            if synth is None:
                raise ApiError(501, "TTS_UNAVAILABLE", "Gateway chưa có bộ đọc giọng (TTS).")
            try:
                pcm = await asyncio.to_thread(synth.synthesize, text, str(b.get("voice") or ""), str(b.get("rate") or ""))
            except Exception as exc:  # noqa: BLE001 - lỗi dịch vụ giọng báo rõ
                raise ApiError(502, "TTS_FAILED", "Không đọc được giọng: %s" % type(exc).__name__)
        rt.run(c.say(pcm))
        return {"seconds": round(len(pcm) / 96000, 1)}

    @app.post("/api/live/accept")
    async def live_accept(request: Request):
        auth(request)
        b = await body_dict(request)
        rt = runtime["rt"]
        c = rt.calls.get(str(b.get("id"))) if rt else None
        if c is None or c.outgoing:
            raise not_found("Không có cuộc gọi đến này.")
        # Cuộc gọi ĐẾN được tạo với mic/camera «test» (âm 440 Hz, hình thử): áp thiết bị mặc định đã đặt trước khi nghe máy,
        # để nguồn mic/camera/loa giống cuộc gọi đi (media chỉ dựng sau khi nghe máy, lúc callStateReady).
        apply_defaults(c, load_settings())
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
