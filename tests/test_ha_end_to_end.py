"""Đầu-cuối TRONG Home Assistant thật: gọi dịch vụ ``telegram_call.*`` bằng ``hass.services.async_call`` (đúng đường mà
automation / Công cụ nhà phát triển dùng), gateway là máy chủ giả mô phỏng các tuyến thật. Kiểm: yêu cầu gateway nhận được,
phản hồi trả về automation, sự kiện tin đến, cảm biến, lỗi.

Chỉ chạy được khi có HA + pytest-homeassistant-custom-component (ảnh ``r1-test``); môi trường khác tự bỏ qua:

    python -m pytest tests/test_ha_end_to_end.py -p pytest_homeassistant_custom_component
"""
import asyncio
import sys
import types

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")
sys.modules.setdefault("turbojpeg", types.SimpleNamespace(TurboJPEG=object))     # camera của HA nạp thư viện này; không cần thật

# Plugin thử của HA đã nạp sẵn một gói ``custom_components`` riêng (thư mục testing_config) và che mất thư mục của kho này:
# thêm thư mục của kho vào gói đó để HA tìm thấy ``telegram_call``.
import os  # noqa: E402
import custom_components  # noqa: E402

_REPO_CC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components")
if os.path.abspath(_REPO_CC) not in [os.path.abspath(p) for p in custom_components.__path__]:
    custom_components.__path__.append(os.path.abspath(_REPO_CC))

from aiohttp import web  # noqa: E402
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events  # noqa: E402
import voluptuous as vol  # noqa: E402

DOMAIN = "telegram_call"
TOKEN = "TOKEN-GIA"


class FakeGateway:
    """Mô phỏng telegram-gateway: ghi lại mọi yêu cầu, trả dữ liệu cố định, có /api/feed long-poll ngắn."""

    def __init__(self):
        self.requests: list[dict] = []
        self.feed_items: list[dict] = [{"id": 1, "type": "message", "message": {"id": 1, "chat_id": 9, "text": "tin cũ", "outgoing": False}}]
        self.fail_next: tuple[int, dict] | None = None
        self.runner = None
        self.url = ""

    def last(self, path_part: str) -> dict:
        return [r for r in self.requests if path_part in r["path"]][-1]

    async def _any(self, request: web.Request) -> web.Response:
        raw = await request.read()
        body = None
        if raw and request.content_type == "application/json":
            body = await request.json()
        rec = {"method": request.method, "path": request.path, "query": dict(request.query), "json": body,
               "raw": raw if body is None else b"", "auth": request.headers.get("Authorization")}
        if request.path != "/api/feed":
            self.requests.append(rec)
        if self.fail_next and request.path.startswith("/api/chats"):
            status, payload = self.fail_next
            self.fail_next = None
            return web.json_response(payload, status=status)
        p, q = request.path, request.query
        if p == "/health":
            return web.json_response({"status": "ok"})
        if p == "/api/accounts":
            return web.json_response({"tai_khoan": [{"phone": "+84900000000"}]})
        if p == "/api/live/calls":
            return web.json_response({"calls": []})
        if p == "/api/live/settings":
            return web.json_response({"ok": True})
        if p == "/api/feed":
            since, wait = int(q.get("since", 0)), min(float(q.get("wait", 0)), 1.0)
            end = asyncio.get_event_loop().time() + wait
            while True:
                items = [i for i in self.feed_items if i["id"] > since]
                if items or wait <= 0 or asyncio.get_event_loop().time() >= end:
                    return web.json_response({"items": items, "cursor": self.feed_items[-1]["id"], "reset": False})
                await asyncio.sleep(0.05)
        if p == "/api/resolve":
            return web.json_response({"chat_id": {"@ben_bap": 6518712943}.get(body["peer"], 42)})
        if p.endswith("/send") or p.endswith("/send_file") or p == "/api/messages/edit":
            return web.json_response({"id": 500, "chat_id": 42, "kind": "text", "text": (body or {}).get("text", ""), "date": 1, "outgoing": True})
        if p == "/api/send_voice":
            return web.json_response({"ok": True, "tin_id": "900", "mock": False})
        if p in ("/api/messages/delete", "/api/messages/forward", "/api/messages/react") or p.endswith("/read"):
            return web.json_response({"ok": True})
        if p.endswith("/messages"):
            return web.json_response({"messages": [{"id": 3, "chat_id": 42, "kind": "text", "text": "xin chào", "date": 5, "outgoing": False, "sender": "Bình"}]})
        if p == "/api/chats":
            return web.json_response({"chats": [{"id": 42, "title": "Đại ca", "kind": "private", "unread": 2, "pinned": True, "last": {"text": "ok"}}]})
        if p == "/api/search":
            return web.json_response({"chats": [{"id": 42, "title": "Đại ca", "kind": "private"}]})
        if p == "/api/contacts":
            return web.json_response({"contacts": [{"id": 1, "name": "Đại ca", "phone": "84900000000", "username": "dai_ca"},
                                                   {"id": 2, "name": "Khác", "phone": "84911111111", "username": ""}]})
        if p == "/api/translate":
            return web.json_response({"text": "Hello (%s)" % body["to_language_code"]})
        if p.startswith("/api/file/"):
            return web.Response(body=b"NOI-DUNG-TEP", content_type="application/octet-stream")
        return web.json_response({"error": "khong co tuyen " + p}, status=404)

    async def start(self):
        app = web.Application(client_max_size=70 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._any)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = "http://127.0.0.1:%d" % site._server.sockets[0].getsockname()[1]

    async def stop(self):
        await self.runner.cleanup()


@pytest.fixture
async def gw(socket_enabled):
    g = FakeGateway()
    await g.start()
    yield g
    await g.stop()


@pytest.fixture
async def setup(hass, gw, enable_custom_integrations, tmp_path):
    hass.config.allowlist_external_dirs.add(str(tmp_path))
    entry = MockConfigEntry(domain=DOMAIN, data={"url": gw.url, "token": TOKEN, "account": "+84900000000"}, title="Telegram Call")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    yield entry
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def call(hass, service, data, response=True):
    return await hass.services.async_call(DOMAIN, service, data, blocking=True, return_response=response)


async def test_services_are_registered_with_translations(hass, setup):
    names = set(hass.services.async_services()[DOMAIN])
    assert {"send_message", "send_file", "send_photo", "send_video", "send_audio", "send_voice", "edit_message", "delete_message",
            "forward_message", "add_reaction", "mark_read", "get_history", "get_chats", "find_chat", "get_contacts",
            "translate_text", "download_file", "make_call", "answer_call", "hangup_call"} <= names


async def test_send_message_by_username_formats_replies_and_returns_message(hass, setup, gw):
    res = await call(hass, "send_message", {"peer": "@ben_bap", "message": "*đậm* chào", "format": "markdown", "reply_to": 77})
    assert gw.last("/api/resolve")["json"] == {"peer": "@ben_bap"}
    sent = gw.last("/send")
    assert sent["path"] == "/api/chats/6518712943/send" and sent["auth"] == "Bearer " + TOKEN
    assert sent["json"] == {"text": "*đậm* chào", "reply_to": 77, "parse_mode": "markdown"}
    assert res["message_id"] == 500 and res["chat_id"] == 42 and "path" not in res


async def test_send_message_legacy_text_with_chat_id_needs_no_resolve(hass, setup, gw):
    await call(hass, "send_message", {"chat_id": 42, "text": "cũ"})
    assert gw.last("/send")["path"] == "/api/chats/42/send" and gw.last("/send")["json"] == {"text": "cũ"}
    assert not [r for r in gw.requests if r["path"] == "/api/resolve"]


async def test_send_requires_a_target_and_validates_format(hass, setup):
    with pytest.raises(vol.Invalid):
        await call(hass, "send_message", {"message": "x"})
    with pytest.raises(vol.Invalid):
        await call(hass, "send_message", {"chat_id": 1, "message": "x", "format": "bbcode"})


async def test_send_photo_reads_allowed_path_and_posts_raw_bytes_with_caption(hass, setup, gw, tmp_path):
    f = tmp_path / "anh.png"
    f.write_bytes(b"\x89PNG-DU-LIEU")
    await call(hass, "send_photo", {"chat_id": 42, "file": str(f), "caption": "chú thích", "reply_to": 5})
    r = gw.last("/send_file")
    assert r["query"] == {"account": "+84900000000", "kind": "photo", "name": "anh.png", "caption": "chú thích", "reply_to": "5"}
    assert r["raw"] == b"\x89PNG-DU-LIEU"


async def test_send_file_kinds_and_path_outside_allowlist_is_refused(hass, setup, gw, tmp_path):
    f = tmp_path / "bao-cao.txt"
    f.write_bytes(b"abc")
    await call(hass, "send_file", {"chat_id": 42, "file": str(f)})
    assert gw.last("/send_file")["query"]["kind"] == "document"
    await call(hass, "send_file", {"chat_id": 42, "file": str(f), "kind": "audio"})
    assert gw.last("/send_file")["query"]["kind"] == "audio"
    n = len(gw.requests)
    with pytest.raises(ServiceValidationError):
        await call(hass, "send_file", {"chat_id": 42, "file": "/etc/passwd"})
    assert len(gw.requests) == n, "đường dẫn ngoài thư mục được phép không được tới gateway"


async def test_send_voice_from_file_and_from_tts_text(hass, setup, gw, tmp_path):
    f = tmp_path / "tts.mp3"
    f.write_bytes(b"ID3-MP3")
    await call(hass, "send_voice", {"chat_id": 42, "file": str(f)})
    assert gw.last("/send_file")["query"]["kind"] == "voice"
    res = await call(hass, "send_voice", {"chat_id": 42, "message": "Xin chào đại ca", "voice": "vi-VN-HoaiMyNeural"})
    assert gw.last("/api/send_voice")["json"] == {"peer": "42", "tts_text": "Xin chào đại ca", "tts_voice": "vi-VN-HoaiMyNeural"}
    assert res["message_id"] == "900"
    with pytest.raises(ServiceValidationError):
        await call(hass, "send_voice", {"chat_id": -1001, "message": "nhóm"})


async def test_edit_delete_forward_react_read(hass, setup, gw):
    await call(hass, "edit_message", {"chat_id": 42, "message_id": 8, "message": "<b>sửa</b>", "format": "html"})
    assert gw.last("/messages/edit")["json"] == {"chat_id": 42, "message_id": 8, "text": "<b>sửa</b>", "parse_mode": "html"}
    await call(hass, "delete_message", {"chat_id": 42, "message_ids": "8, 9", "revoke": False}, response=False)
    assert gw.last("/messages/delete")["json"] == {"chat_id": 42, "message_ids": [8, 9], "revoke": False}
    await call(hass, "forward_message", {"peer": "@ben_bap", "from_chat_id": 42, "message_ids": [8]}, response=False)
    assert gw.last("/messages/forward")["json"] == {"to_chat_id": 6518712943, "from_chat_id": 42, "message_ids": [8]}
    await call(hass, "add_reaction", {"chat_id": 42, "message_id": 8, "emoji": "🔥"}, response=False)
    assert gw.last("/messages/react")["json"] == {"chat_id": 42, "message_id": 8, "emoji": "🔥", "on": True}
    await call(hass, "add_reaction", {"chat_id": 42, "message_id": 8, "remove": True}, response=False)
    assert gw.last("/messages/react")["json"]["on"] is False
    await call(hass, "mark_read", {"chat_id": 42, "message_ids": "3"}, response=False)
    assert gw.last("/read")["json"] == {"message_ids": [3]}
    with pytest.raises(vol.Invalid):
        await call(hass, "delete_message", {"chat_id": 42, "message_ids": ""}, response=False)


async def test_read_services_return_data_to_the_automation(hass, setup, gw):
    h = await call(hass, "get_history", {"chat_id": 42, "limit": 5})
    assert h["messages"][0]["text"] == "xin chào" and h["messages"][0]["sender"] == "Bình"
    assert gw.last("/messages")["query"]["limit"] == "5"
    c = await call(hass, "get_chats", {})
    assert c["chats"][0] == {"id": 42, "title": "Đại ca", "kind": "private", "unread": 2, "pinned": True, "last": "ok"}
    assert (await call(hass, "find_chat", {"query": "đại"}))["chats"][0]["id"] == 42
    ct = await call(hass, "get_contacts", {"query": "đại"})
    assert [x["id"] for x in ct["contacts"]] == [1] and "phone" not in ct["contacts"][0], "danh bạ trả về không kèm số điện thoại"
    t = await call(hass, "translate_text", {"text": "Xin chào", "to_language": "en"})
    assert t == {"text": "Hello (en)"} and gw.last("/api/translate")["json"] == {"text": "Xin chào", "to_language_code": "en"}


async def test_download_file_writes_only_inside_allowed_dirs(hass, setup, gw, tmp_path):
    dest = tmp_path / "nhan.bin"
    res = await call(hass, "download_file", {"file_id": 12, "path": str(dest)})
    assert dest.read_bytes() == b"NOI-DUNG-TEP" and res == {"path": str(dest), "bytes": 12}
    with pytest.raises(ServiceValidationError):
        await call(hass, "download_file", {"file_id": 12, "path": "/etc/cron.d/xau"})


async def test_gateway_error_becomes_a_readable_home_assistant_error(hass, setup, gw):
    gw.fail_next = (400, {"error": {"code": "TELEGRAM_ERROR", "message": "Telegram từ chối: Can't parse entities"}})
    with pytest.raises(HomeAssistantError, match="Can't parse entities"):
        await call(hass, "send_message", {"chat_id": 42, "message": "**", "format": "markdown"})


async def test_incoming_message_fires_event_updates_sensor_and_skips_old_and_own(hass, setup, gw):
    events = async_capture_events(hass, "telegram_call_message")
    await asyncio.sleep(0.3)                                       # vòng nghe đã lấy mốc, bỏ qua «tin cũ»
    gw.feed_items += [
        {"id": 2, "type": "message", "message": {"id": 20, "chat_id": 42, "outgoing": True, "text": "tin mình gửi"}},
        {"id": 3, "type": "message", "message": {"id": 21, "chat_id": 42, "outgoing": False, "sender": "Đại ca", "kind": "photo", "text": "xem ảnh",
                                                 "summary": "🖼 Ảnh", "reply_to": 5, "date": 99, "file": {"id": 77, "name": "a.jpg", "mime": "image/jpeg", "local_path": "/data/x"}}},
    ]
    for _ in range(40):
        if events:
            break
        await asyncio.sleep(0.1)
    await hass.async_block_till_done()
    assert len(events) == 1, [e.data for e in events]
    ev = events[0].data
    assert ev == {"chat_id": 42, "message_id": 21, "sender": "Đại ca", "kind": "photo", "text": "xem ảnh", "summary": "🖼 Ảnh",
                  "reply_to": 5, "date": 99, "file_id": 77, "file_name": "a.jpg", "mime": "image/jpeg"}
    st = hass.states.get("sensor.telegram_call_last_message")
    assert st is not None and st.state == "xem ảnh" and st.attributes["file_id"] == 77 and st.attributes["received"] == 1


async def test_send_failed_item_fires_event(hass, setup, gw):
    events = async_capture_events(hass, "telegram_call_send_failed")
    await asyncio.sleep(0.3)
    gw.feed_items.append({"id": 2, "type": "send_failed", "chat_id": 42, "old_id": 9, "error": "FILE_PART_INVALID"})
    for _ in range(40):
        if events:
            break
        await asyncio.sleep(0.1)
    assert events and events[0].data == {"chat_id": 42, "old_message_id": 9, "error": "FILE_PART_INVALID"}
