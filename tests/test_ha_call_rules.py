"""Telegram Call: quy tắc cuộc gọi (apply_call_rule / set_rule), nghe máy theo quy tắc, nói trực tiếp, tắt mic/hình, đọc lời, cảnh báo, người nhận (notify), thực thể mới
— cùng mô hình với zalo_call (tests/test_zalo_call_voice.py). Chạy trong HA thật:

    python -m pytest tests/test_ha_call_rules.py -p pytest_homeassistant_custom_component -o asyncio_mode=auto"""
import asyncio
import time

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from aiohttp import web  # noqa: E402
from homeassistant.core import callback  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from test_ha_end_to_end import DOMAIN, FakeGateway, call, setup  # noqa: E402,F401

from custom_components.telegram_call import const  # noqa: E402

GRACE = 1.2


class CallGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []
        self.status = "online"
        self.say_seconds = 0.2

    async def _any(self, request: web.Request) -> web.Response:
        p = request.path
        if p == "/api/live/calls":
            return web.json_response({"calls": self.calls})
        if p == "/api/accounts":
            return web.json_response({"tai_khoan": [{"phone": "+84900000000", "display_name": "Chủ nhà", "status": self.status}]})
        if p in ("/api/live/accept", "/api/live/hangup", "/api/live/media", "/api/live/say", "/api/live/playback", "/api/live/call"):
            raw = await request.read()
            import json
            body = json.loads(raw) if raw else {}
            self.requests.append({"method": "POST", "path": p, "json": body, "query": {}, "raw": b"", "auth": None})
            if p == "/api/live/say":
                return web.json_response({"seconds": self.say_seconds})
            if p == "/api/live/call":
                new = {"id": "n1", "user_id": body.get("user_id", 7), "outgoing": True, "video": bool(body.get("video")), "state": "dialing"}
                self.calls.append(new)
                return web.json_response(new)
            return web.json_response({"ok": True, "id": body.get("id"), "state": "active"})
        return await super()._any(request)


@pytest.fixture
async def gw(socket_enabled):
    g = CallGateway()
    await g.start()
    yield g
    await g.stop()


RINGING = {"id": "c1", "user_id": 5, "outgoing": False, "video": False, "state": "ringing"}
ACTIVE = dict(RINGING, state="active")


async def until(cond, secs=8.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


def reqs(gw, path):
    return [r["json"] for r in gw.requests if r["path"] == path]


def _rt(entry):
    rt = entry.runtime_data
    rt.voice.decide_grace = GRACE
    return rt


async def _ring(gw, rt, call_dict=RINGING):
    gw.calls = [dict(call_dict)]
    assert await until(lambda: call_dict["id"] in rt.voice.incoming), "tích hợp chưa đánh dấu cuộc gọi đến"


# -- nghe máy theo quy tắc -------------------------------------------------------------------------------------

async def test_no_rule_means_the_call_just_rings(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await asyncio.sleep(GRACE + 0.8)
    assert not reqs(gw, "/api/live/accept")


async def test_apply_call_rule_answers_with_held_devices_after_the_grace_period(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    res = await call(hass, "apply_call_rule", {"call_id": "c1", "source": "automation.a", "answer": True, "answer_delay": 0})
    assert res["status"] in ("queued", "recorded") and res["scope"] == "thread"
    assert not reqs(gw, "/api/live/accept"), "phải chờ hết thời gian chốt rồi mới nghe"
    assert await until(lambda: reqs(gw, "/api/live/accept"))
    held = reqs(gw, "/api/live/settings")[-1]
    assert held["mic"] == "silence" and held["camera"] == "black" and held["speaker"] == "", held      # không nối gì của nhà: im lặng, hình đen


async def test_connect_now_wires_only_the_ticked_parts(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await call(hass, "apply_call_rule", {"call_id": "c1", "source": "automation.a", "answer": True, "answer_delay": 0,
                                         "connect_now": ["mic"], "mic_url": "http://r1.local/mic.pcm", "camera": "camera.khong_co"})
    assert await until(lambda: reqs(gw, "/api/live/accept"))
    s = reqs(gw, "/api/live/settings")[-1]
    assert s["mic"] == "http://r1.local/mic.pcm" and s["camera"] == "black", s                    # camera KHÔNG tích thì không nối dù có ghi trong quy tắc


async def test_most_specific_rule_wins_and_a_refusing_person_rule_blocks_a_lenient_group_rule(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await call(hass, "apply_call_rule", {"call_id": "c1", "scope": "contacts", "source": "automation.cho_phep", "answer": True, "answer_delay": 0})
    await call(hass, "apply_call_rule", {"call_id": "c1", "scope": "thread", "source": "automation.chan", "answer": False})
    await asyncio.sleep(GRACE + 1.0)
    assert not reqs(gw, "/api/live/accept"), "quy tắc theo người (answer false) phải thay quy tắc danh bạ dễ dãi"


async def test_two_rules_same_rank_first_with_answer_is_primary_and_duplicate_source_is_ignored(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await call(hass, "apply_call_rule", {"call_id": "c1", "source": "automation.bao", "answer": False})
    r = await call(hass, "apply_call_rule", {"call_id": "c1", "source": "automation.nghe", "answer": True, "answer_delay": 0, "connect_now": ["mic"], "mic_url": "http://r1.local/a"})
    assert r["status"] == "queued"
    assert (await call(hass, "apply_call_rule", {"call_id": "c1", "source": "automation.nghe", "answer": True}))["status"] == "duplicate"
    assert await until(lambda: reqs(gw, "/api/live/accept"))
    assert reqs(gw, "/api/live/settings")[-1]["mic"] == "http://r1.local/a"
    assert len(reqs(gw, "/api/live/accept")) == 1


async def test_stored_rule_is_only_a_fallback_and_never_runs_when_an_automation_claimed_the_call(hass, setup, gw):
    rt = _rt(setup)
    await call(hass, "set_rule", {"scope": "thread", "thread_id": "5", "answer": True, "answer_delay": 0})
    await _ring(gw, rt)
    assert await until(lambda: reqs(gw, "/api/live/accept")), "không có automation: quy tắc lưu sẵn dự phòng phải chạy"
    gw.calls, gw.requests[:] = [dict(RINGING, id="c2", state="ringing")], []
    rt.voice.incoming.discard("c2")
    assert await until(lambda: "c2" in rt.voice.incoming)
    await call(hass, "apply_call_rule", {"call_id": "c2", "source": "automation.x", "answer": False})
    await asyncio.sleep(GRACE + 1.0)
    assert not reqs(gw, "/api/live/accept"), "đã có quy tắc từ automation thì quy tắc lưu sẵn không được áp (tránh xử lý hai lần)"


async def test_call_types_limit_the_rule(hass, setup, gw):
    rt = _rt(setup)
    await call(hass, "set_rule", {"scope": "thread", "thread_id": "5", "answer": True, "answer_delay": 0, "call_types": ["voice"]})
    await _ring(gw, rt, dict(RINGING, video=True))
    await asyncio.sleep(GRACE + 1.0)
    assert not reqs(gw, "/api/live/accept")


async def test_event_carries_contact_status_name_and_tags(hass, setup, gw):
    seen = []

    @callback
    def _on(e):
        seen.append(dict(e.data))
    hass.bus.async_listen(const.EVENT_STATE, _on)
    await call(hass, "set_contact_tags", {"thread_id": "1", "tags": "Gia đình, vip"})
    gw.calls = [dict(RINGING, id="f", user_id=1)]
    assert await until(lambda: any(e["call_id"] == "f" for e in seen))
    e = [e for e in seen if e["call_id"] == "f"][0]
    assert (e["contact_status"], e["name"], e["tags"], e["call_type"]) == ("friend", "Đại ca", ["gia đình", "vip"], "voice"), e
    gw.calls = [dict(RINGING, id="s", user_id=99, video=True)]
    assert await until(lambda: any(e["call_id"] == "s" for e in seen))
    e = [e for e in seen if e["call_id"] == "s"][0]
    assert (e["contact_status"], e["name"], e["call_type"]) == ("stranger", "", "video"), e


async def test_tag_rule_applies_to_a_tagged_person(hass, setup, gw):
    rt = _rt(setup)
    await call(hass, "set_contact_tags", {"thread_id": "5", "tags": "gia đình"})
    await call(hass, "set_rule", {"scope": "tag", "tag": "Gia Đình", "answer": True, "answer_delay": 0})
    await _ring(gw, rt)
    assert await until(lambda: reqs(gw, "/api/live/accept"))


# -- giữa cuộc gọi ---------------------------------------------------------------------------------------------

async def _answered(hass, gw, rt, **rule):
    await call(hass, "set_rule", {"scope": "thread", "thread_id": "5", "answer": True, "answer_delay": 0, **rule})
    await _ring(gw, rt)
    assert await until(lambda: reqs(gw, "/api/live/accept"))
    gw.calls = [dict(ACTIVE)]
    assert await until(lambda: "c1" in rt.voice.answered_ids)


async def test_take_over_wires_the_rule_devices_mid_call(hass, setup, gw):
    rt = _rt(setup)
    await _answered(hass, gw, rt, connect=["mic"], mic_url="http://r1.local/mic.pcm")
    res = await call(hass, "take_over_call", {})
    assert res == {"call_id": "c1", "connected": ["mic"]}
    assert reqs(gw, "/api/live/media")[-1]["mic"] == "http://r1.local/mic.pcm"


async def test_take_over_without_any_device_says_so(hass, setup, gw):
    rt = _rt(setup)
    await _answered(hass, gw, rt)
    from homeassistant.exceptions import HomeAssistantError
    with pytest.raises(HomeAssistantError, match="Chưa chọn"):
        await call(hass, "take_over_call", {})


async def test_mute_mic_and_video_and_start_mic(hass, setup, gw):
    rt = _rt(setup)
    await _answered(hass, gw, rt)
    await call(hass, "stop_call_mic", {})
    assert reqs(gw, "/api/live/media")[-1] == {"id": "c1", "mic": "silence"}
    await call(hass, "stop_call_video", {})
    assert reqs(gw, "/api/live/media")[-1] == {"id": "c1", "camera_off": True}
    await call(hass, "start_call_mic", {})
    assert reqs(gw, "/api/live/media")[-1]["mic"] == "silence"                   # chưa chọn mic mặc định: im lặng (không phải âm thử), giống make_call


async def test_say_in_call_reads_text_into_the_call(hass, setup, gw):
    rt = _rt(setup)
    await _answered(hass, gw, rt)
    res = await call(hass, "say_in_call", {"message": "Xin chào", "wait": False})
    assert res["call_id"] == "c1" and res["seconds"] == 0.2
    body = reqs(gw, "/api/live/say")[-1]
    assert body["id"] == "c1" and (body.get("text") == "Xin chào" or body.get("audio_b64"))


async def test_reject_and_end_call_hang_up(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await call(hass, "reject_call", {})
    assert reqs(gw, "/api/live/hangup")[-1] == {"id": "c1"}
    await call(hass, "end_call", {"call_id": "zz"})
    assert reqs(gw, "/api/live/hangup")[-1] == {"id": "zz"}


async def test_greeting_is_read_after_answering_then_hangs_up(hass, setup, gw):
    rt = _rt(setup)
    await call(hass, "set_rule", {"scope": "thread", "thread_id": "5", "answer": True, "answer_delay": 0, "greeting": "Nhà đi vắng, để lại lời nhắn", "hangup_after": True})
    await _ring(gw, rt)
    assert await until(lambda: reqs(gw, "/api/live/accept"))
    gw.calls = [dict(ACTIVE)]
    assert await until(lambda: reqs(gw, "/api/live/hangup"), 10)
    assert reqs(gw, "/api/live/say") and reqs(gw, "/api/live/hangup")[-1] == {"id": "c1"}


async def test_alert_call_dials_with_everything_off_reads_then_hangs_up_and_announces(hass, setup, gw):
    rt = _rt(setup)
    events = []

    @callback
    def _on(e):
        events.append(dict(e.data))
    hass.bus.async_listen("telegram_call_alert_done", _on)
    res = await call(hass, "alert_call", {"user_id": 7, "message": "Có khói trong bếp", "after": "hangup"})
    assert res["id"] == "n1"
    dial = reqs(gw, "/api/live/call")[-1]
    assert dial["speaker"] == "" and dial["mic"] == "silence", dial                     # nghe máy xong chưa nối gì của nhà
    gw.calls = [dict(gw.calls[-1], state="active")]
    assert await until(lambda: events, 12)
    assert reqs(gw, "/api/live/say") and reqs(gw, "/api/live/hangup")[-1] == {"id": "n1"}
    assert events[0]["after"] == "hangup" and events[0]["ended"] is True and events[0]["user_id"] == 7


# -- kiểm dữ liệu vào ------------------------------------------------------------------------------------------

async def test_rule_validation_errors_are_readable(hass, setup, gw):
    for data, text in (({"scope": "thread", "answer": True}, "thread_id"),
                       ({"scope": "tag"}, "tag"),
                       ({"scope": "contacts", "announce": True}, "announce_to"),
                       ({"scope": "contacts", "hangup_after": True}, "greeting"),
                       ({"scope": "contacts", "connect": ["loa"]}, "connect")):
        with pytest.raises((ServiceValidationError, Exception)) as err:
            await call(hass, "set_rule", data)
        assert text in str(err.value), (data, str(err.value))


async def test_set_list_remove_rule_roundtrip(hass, setup, gw):
    await call(hass, "set_rule", {"scope": "strangers", "answer": False, "announce": True, "announce_to": "media_player.r1"})
    await call(hass, "set_rule", {"scope": "thread", "thread_id": "5", "answer": True})
    listed = await call(hass, "list_rules", {})
    assert listed["threads"]["5"]["answer"] is True and listed["strangers"]["announce_to"] == "media_player.r1" and listed["contacts"] is None
    assert (await call(hass, "remove_rule", {"scope": "thread", "thread_id": "5"}))["removed"] is True
    assert (await call(hass, "remove_rule", {"scope": "thread", "thread_id": "5"}))["removed"] is False
    assert (await call(hass, "list_rules", {}))["threads"] == {}
    d = await call(hass, "get_default_devices", {})
    assert set(d["devices"]) >= {"mic", "speaker", "camera"} and "call_volume" in d


async def test_rules_survive_a_restart(hass, setup, gw):
    await call(hass, "set_rule", {"scope": "contacts", "answer": True, "answer_delay": 5})
    await hass.config_entries.async_reload(setup.entry_id)
    await hass.async_block_till_done()
    assert (await call(hass, "list_rules", {}))["contacts"]["answer_delay"] == 5


# -- thực thể --------------------------------------------------------------------------------------------------

async def test_new_entities_exist_like_zalo_call(hass, setup, gw):
    reg = er.async_get(hass)
    for platform, key in (("button", "btn_take_over"), ("button", "btn_stop_mic"), ("button", "btn_stop_video"), ("button", "hangup"),
                          ("binary_sensor", "logged_in"), ("sensor", "account")):
        assert reg.async_get_entity_id(platform, DOMAIN, "%s_%s" % (setup.entry_id, key)), key
    st = hass.states.get(reg.async_get_entity_id("binary_sensor", DOMAIN, "%s_logged_in" % setup.entry_id))
    assert st.state == "on" and st.attributes["display_name"] == "Chủ nhà"
    assert hass.states.get(reg.async_get_entity_id("sensor", DOMAIN, "%s_account" % setup.entry_id)).state == "online"


async def test_buttons_control_the_active_call(hass, setup, gw):
    rt = _rt(setup)
    await _answered(hass, gw, rt)
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("button", DOMAIN, "%s_btn_stop_mic" % setup.entry_id)
    await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
    assert reqs(gw, "/api/live/media")[-1] == {"id": "c1", "mic": "silence"}
    eid = reg.async_get_entity_id("button", DOMAIN, "%s_btn_stop_video" % setup.entry_id)
    await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
    assert reqs(gw, "/api/live/media")[-1] == {"id": "c1", "camera_off": True}


async def test_contacts_become_notify_entities_and_options_flow_picks_recipients(hass, setup, gw):
    reg = er.async_get(hass)
    eid = reg.async_get_entity_id("notify", DOMAIN, "%s_to_1" % setup.entry_id)
    assert eid and reg.async_get(eid).hidden_by is not None, "người chưa được chọn: thực thể có nhưng ẨN khỏi bảng điều khiển"
    assert reg.async_get_entity_id("notify", DOMAIN, "%s_to_2" % setup.entry_id)
    form = await hass.config_entries.options.async_init(setup.entry_id)
    assert form["type"] == "form" and form["step_id"] == "init"
    from homeassistant.data_entry_flow import InvalidData
    with pytest.raises(InvalidData):                                                          # ID không có trong danh bạ bị từ chối
        await hass.config_entries.options.async_configure(form["flow_id"], {"recipients": ["1", "999"]})
    done = await hass.config_entries.options.async_configure(form["flow_id"], {"recipients": ["1"]})
    assert done["type"] == "create_entry" and done["data"]["recipients"] == ["1"]
    await hass.async_block_till_done()
    eid = reg.async_get_entity_id("notify", DOMAIN, "%s_to_1" % setup.entry_id)
    assert reg.async_get(eid).hidden_by is None
    st = hass.states.get(eid)
    assert st is not None and st.attributes["user_id"] == "1" and st.attributes["chat_id"] == "1"
    await hass.services.async_call("notify", "send_message", {"entity_id": eid, "message": "chào"}, blocking=True)
    assert gw.last("/send")["json"]["text"] == "chào"


# -- xin chuyển sang video (người gọi bật camera giữa cuộc thoại) ----------------------------------------------------

async def test_peer_turning_the_camera_on_fires_video_request_once(hass, setup, gw):
    rt = _rt(setup)
    seen = []

    @callback
    def _on(e):
        seen.append(dict(e.data))
    hass.bus.async_listen(const.EVENT_STATE, _on)
    await _answered(hass, gw, rt)
    gw.calls = [dict(ACTIVE, peer_video=True)]
    assert await until(lambda: any(e["state"] == "video_request" for e in seen))
    await asyncio.sleep(2.5)
    assert len([e for e in seen if e["state"] == "video_request"]) == 1               # bật liên tục chỉ báo MỘT lần
    e = [e for e in seen if e["state"] == "video_request"][0]
    assert e["call_id"] == "c1" and e["user_id"] == 5 and e["call_type"] == "voice"
    assert not reqs(gw, "/api/live/media"), "mặc định «ask»: chỉ báo sự kiện, không tự đồng ý"


async def test_video_request_accept_sends_black_and_accept_camera_falls_back_to_black_without_a_stream(hass, setup, gw):
    rt = _rt(setup)
    await _answered(hass, gw, rt, video_request="accept")
    gw.calls = [dict(ACTIVE, peer_video=True)]
    assert await until(lambda: reqs(gw, "/api/live/media"))
    assert reqs(gw, "/api/live/media")[-1] == {"id": "c1", "video": True, "camera": "black", "camera_off": False}


async def test_accept_video_service_and_button_turn_the_home_video_on(hass, setup, gw):
    rt = _rt(setup)
    await _answered(hass, gw, rt)
    res = await call(hass, "accept_video", {})
    assert res == {"call_id": "c1", "video": True, "camera": False}
    assert reqs(gw, "/api/live/media")[-1]["video"] is True
    gw.requests[:] = []
    eid = er.async_get(hass).async_get_entity_id("button", DOMAIN, "%s_btn_upgrade_video" % setup.entry_id)
    await hass.services.async_call("button", "press", {"entity_id": eid}, blocking=True)
    assert reqs(gw, "/api/live/media")[-1] == {"id": "c1", "video": True, "camera": "black", "camera_off": False}
    gw.requests[:] = []
    await call(hass, "start_call_video", {})                                           # bật hình ngay cả khi cuộc đang là thoại
    assert reqs(gw, "/api/live/media")[-1]["video"] is True


async def test_video_request_rule_field_is_validated(hass, setup, gw):
    with pytest.raises(Exception) as err:
        await call(hass, "set_rule", {"scope": "contacts", "video_request": "tu_dong"})
    assert "video_request" in str(err.value)
    await call(hass, "set_rule", {"scope": "contacts", "video_request": "accept_camera"})
    assert (await call(hass, "list_rules", {}))["contacts"]["video_request"] == "accept_camera"


# -- nghe máy bằng TAY (nút R1, answer_call...): thiết bị vẫn được nối theo quy tắc ------------------------------------

RULE_DEVICES = {"answer": False, "connect_now": ["mic", "speaker"], "mic_url": "http://r1.local/mic.pcm"}


async def _answer_by_hand(hass, gw, rt, answer_data=None):
    await call(hass, "answer_call", {"call_id": "c1", **(answer_data or {})})
    gw.calls = [dict(ACTIVE)]
    assert await until(lambda: "c1" in rt.voice.answered_ids)


async def test_hand_answered_call_gets_the_rule_devices_wired_when_it_becomes_active(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await call(hass, "apply_call_rule", {"call_id": "c1", "source": "automation.a", **RULE_DEVICES})
    await asyncio.sleep(GRACE + 0.6)
    assert not reqs(gw, "/api/live/accept"), "quy tắc không cho tự nghe máy"
    await _answer_by_hand(hass, gw, rt)
    assert await until(lambda: reqs(gw, "/api/live/media")), "ai nghe máy thì thiết bị của quy tắc cũng phải được nối"
    assert reqs(gw, "/api/live/media")[-1]["mic"] == "http://r1.local/mic.pcm"
    assert len(reqs(gw, "/api/live/media")) == 1, "mỗi cuộc chỉ nối một lần"


async def test_hand_answered_call_without_any_rule_connects_nothing(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await asyncio.sleep(GRACE + 0.4)
    await _answer_by_hand(hass, gw, rt)
    await asyncio.sleep(0.8)
    assert not reqs(gw, "/api/live/media") and not reqs(gw, "/api/live/playback")


async def test_rule_answered_call_is_not_wired_a_second_time(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await call(hass, "apply_call_rule", {"call_id": "c1", "source": "automation.a", "answer": True, "answer_delay": 0,
                                         "connect_now": ["mic"], "mic_url": "http://r1.local/mic.pcm"})
    assert await until(lambda: reqs(gw, "/api/live/accept"))
    gw.calls = [dict(ACTIVE)]
    assert await until(lambda: "c1" in rt.voice.answered_ids)
    await asyncio.sleep(0.8)
    assert not reqs(gw, "/api/live/media"), "nghe theo quy tắc đã nối thiết bị lúc nghe máy (settings), không nối lại"


async def test_hand_answer_with_its_own_devices_keeps_them_and_skips_the_rule(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await call(hass, "apply_call_rule", {"call_id": "c1", "source": "automation.a", **RULE_DEVICES})
    await asyncio.sleep(GRACE + 0.4)
    await _answer_by_hand(hass, gw, rt, {"mic_url": "http://khac.local/mic"})
    await asyncio.sleep(0.8)
    assert not reqs(gw, "/api/live/media"), "answer_call kèm thiết bị riêng thì quy tắc không đè lên"


async def test_rule_that_arrives_after_the_call_was_answered_still_wires_the_devices(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await asyncio.sleep(GRACE + 0.4)                              # hết giờ chờ, chưa có quy tắc nào
    await _answer_by_hand(hass, gw, rt)
    assert not reqs(gw, "/api/live/media")
    res = await call(hass, "apply_call_rule", {"call_id": "c1", "source": "automation.cham", **RULE_DEVICES})
    assert res["status"] == "connected"
    assert await until(lambda: reqs(gw, "/api/live/media"))
    assert reqs(gw, "/api/live/media")[-1]["mic"] == "http://r1.local/mic.pcm"


async def test_outgoing_calls_are_never_wired_by_an_incoming_rule(hass, setup, gw):
    rt = _rt(setup)
    await call(hass, "set_rule", {"scope": "thread", "thread_id": "5", **RULE_DEVICES})
    gw.calls = [dict(ACTIVE, outgoing=True)]
    assert await until(lambda: "c1" in rt.voice.answered_ids)
    await asyncio.sleep(0.8)
    assert not reqs(gw, "/api/live/media")


# -- make_call / answer_call: ô chọn sẵn «nối ngay» và «khi nói trực tiếp» (như zalo_call) ---------------------------------

async def test_make_call_connect_now_wires_only_the_ticked_parts_and_camera_means_video(hass, setup, gw):
    _rt(setup)
    await call(hass, "make_call", {"user_id": 5, "mic_url": "http://r1.local/mic.pcm", "connect_now": ["mic"]})
    b = reqs(gw, "/api/live/call")[-1]
    assert b["mic"] == "http://r1.local/mic.pcm" and b["camera"] == "black" and b["speaker"] == "" and b["video"] is False, b
    gw.requests.clear()
    gw.calls = []
    await call(hass, "make_call", {"user_id": 6, "mic_url": "http://r1.local/mic.pcm", "connect_now": ["camera"]})
    b = reqs(gw, "/api/live/call")[-1]
    assert b["video"] is True and b["mic"] == "silence", b          # tích camera = gọi VIDEO; mic không tích thì im lặng, dù đã chọn mic_url


async def test_make_call_without_connect_now_keeps_the_old_behaviour(hass, setup, gw):
    _rt(setup)
    await call(hass, "make_call", {"user_id": 5, "mic_url": "http://r1.local/mic.pcm"})
    b = reqs(gw, "/api/live/call")[-1]
    assert b["mic"] == "http://r1.local/mic.pcm" and "speaker" not in b and b["video"] is False, b


async def test_make_call_remembers_its_own_take_over_devices(hass, setup, gw):
    rt = _rt(setup)
    await call(hass, "make_call", {"user_id": 5, "mic_url": "http://r1.local/mic.pcm", "connect_now": [], "connect": ["mic"]})
    assert rt.voice.rule_of["n1"]["connect"] == ["mic"] and rt.voice.rule_of["n1"]["connect_now"] == []
    assert reqs(gw, "/api/live/call")[-1]["mic"] == "silence"                         # chưa nối gì lúc nghe máy
    gw.calls = [dict(ACTIVE, id="n1", user_id=5, outgoing=True)]
    assert await until(lambda: "n1" in rt.voice.answered_ids)
    res = await call(hass, "take_over_call", {"call_id": "n1"})
    assert res["connected"] == ["mic"] and reqs(gw, "/api/live/media")[-1]["mic"] == "http://r1.local/mic.pcm"


async def test_answer_call_connect_now_holds_the_unticked_parts_silent_and_black(hass, setup, gw):
    rt = _rt(setup)
    await _ring(gw, rt)
    await call(hass, "answer_call", {"call_id": "c1", "mic_url": "http://r1.local/mic.pcm", "camera": "camera.khong_co", "connect_now": ["mic"]})
    s = reqs(gw, "/api/live/settings")[-1]
    assert s["mic"] == "http://r1.local/mic.pcm" and s["camera"] == "black", s
    assert reqs(gw, "/api/live/accept")
