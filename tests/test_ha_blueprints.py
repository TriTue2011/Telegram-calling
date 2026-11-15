"""Blueprint Telegram Call chạy THẬT trong HA (nạp đúng file blueprint của kho TriTue2011/Blueprint):
quy tắc cuộc gọi đến (mỗi cuộc kích hoạt automation, tự nghe máy đúng người/đúng giờ) và nút bấm (nghe / cúp / gọi đi).

    python -m pytest tests/test_ha_blueprints.py -p pytest_homeassistant_custom_component -o asyncio_mode=auto

Cần blueprint thật: biến TELEGRAM_BLUEPRINT_DIR, hoặc `bp/` cạnh kho, hoặc `../Blueprint`. Không có thì bỏ qua (nêu lý do)."""
import asyncio
import os
import shutil
import time

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

_HERE = os.path.dirname(os.path.abspath(__file__))
_DIRS = [os.environ.get("TELEGRAM_BLUEPRINT_DIR", ""), os.path.join(_HERE, "..", "bp"), os.path.join(_HERE, "..", "..", "Blueprint")]
BP_DIR = next((d for d in _DIRS if d and os.path.isfile(os.path.join(d, "telegram_call_incoming_rule.yaml"))), "")
pytestmark = pytest.mark.skipif(not BP_DIR, reason="không thấy blueprint thật (đặt TELEGRAM_BLUEPRINT_DIR)")

from aiohttp import web  # noqa: E402
from homeassistant.helpers import entity_registry as er  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from test_ha_realtime_answer import RINGING, CallGateway  # noqa: E402,F401
from test_ha_end_to_end import DOMAIN, call, setup  # noqa: E402,F401


class BlueprintGateway(CallGateway):
    async def _any(self, request: web.Request) -> web.Response:
        if request.path in ("/api/live/call", "/api/live/hangup"):
            self.requests.append({"method": "POST", "path": request.path, "json": await request.json(), "query": {}, "raw": b"", "auth": None})
            return web.json_response({"ok": True, "id": "c9", "state": "dialing"})
        return await super()._any(request)


@pytest.fixture
async def gw(socket_enabled):
    g = BlueprintGateway()
    await g.start()
    yield g
    await g.stop()


def _install(hass):
    dst = hass.config.path("blueprints", "automation", "TriTue2011")
    os.makedirs(dst, exist_ok=True)
    for f in ("telegram_call_incoming_rule.yaml", "telegram_call_nut_bam.yaml"):
        shutil.copy(os.path.join(BP_DIR, f), os.path.join(dst, f))


async def _load(hass, *automations):
    _install(hass)
    assert await async_setup_component(hass, "automation", {"automation": list(automations)})
    await hass.async_block_till_done()


def _sensor(hass, setup):
    return er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "%s_call_state" % setup.entry_id)


def _accepts(gw):
    return [r for r in gw.requests if r["path"] == "/api/live/accept"]


async def _wait(cond, within=4.0):
    end = time.monotonic() + within
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.05)
    return False


def _rule(hass, setup, alias, **inputs):
    return {"alias": alias, "use_blueprint": {"path": "TriTue2011/telegram_call_incoming_rule.yaml",
                                              "input": {"cam_bien": _sensor(hass, setup), **inputs}}}


async def test_incoming_rule_answers_only_the_listed_caller_with_the_chosen_devices(hass, setup, gw):
    await _load(hass, _rule(hass, setup, "Tele A", nguoi_goi=["5"], nghe_sau=0, mic="", am_luong=0))
    gw.calls = [dict(RINGING, id="x1", user_id=77)]                              # người lạ không có trong danh sách
    await asyncio.sleep(3.0)
    assert not _accepts(gw)
    gw.calls = [dict(RINGING, id="12345", user_id=5)]            # id toàn chữ số: HA đọc mẫu thành SỐ, blueprint phải ép chuỗi hai vế
    assert await _wait(lambda: _accepts(gw)), "không nghe máy cho người trong danh sách"
    assert _accepts(gw)[-1]["json"] == {"id": "12345"}
    assert len(_accepts(gw)) == 1


async def test_incoming_rule_waits_the_set_seconds_and_does_not_answer_a_call_that_was_dropped(hass, setup, gw):
    await _load(hass, _rule(hass, setup, "Tele B", moi_nguoi=True, nghe_sau=3))
    gw.calls = [dict(RINGING, id="y1", user_id=9)]
    await asyncio.sleep(2.0)
    assert not _accepts(gw), "nghe máy quá sớm (chưa hết 3 giây)"
    gw.calls = [dict(RINGING, id="y1", user_id=9, state="ended")]                  # người gọi cúp trong lúc chờ
    await asyncio.sleep(3.5)
    assert not _accepts(gw)


async def test_incoming_rule_off_or_disabled_does_nothing(hass, setup, gw):
    await _load(hass, _rule(hass, setup, "Tele C", moi_nguoi=True, nghe_sau=0, bat_quy_tac=False),
                _rule(hass, setup, "Tele D", moi_nguoi=True, nghe_sau=0))
    await hass.services.async_call("automation", "turn_off", {"entity_id": "automation.tele_d"}, blocking=True)
    gw.calls = [dict(RINGING, id="z1", user_id=5)]
    await asyncio.sleep(3.0)
    assert not _accepts(gw)


async def test_button_answers_hangs_up_or_calls_depending_on_the_moment(hass, setup, gw):
    auto = {"alias": "Tele nut", "use_blueprint": {"path": "TriTue2011/telegram_call_nut_bam.yaml", "input": {
        "nut_bam": [{"trigger": "event", "event_type": "tele_nut"}], "cam_bien": _sensor(hass, setup),
        "nguoi_nhan": "@ben_bap", "mic": ""}}}
    await _load(hass, auto)
    sensor = _sensor(hass, setup)
    hass.bus.async_fire("tele_nut")                                              # rảnh: gọi đi
    assert await _wait(lambda: [r for r in gw.requests if r["path"] == "/api/live/call"])
    assert [r for r in gw.requests if r["path"] == "/api/live/call"][-1]["json"]["peer"] == "@ben_bap"
    gw.calls = [dict(RINGING, id="n1", user_id=5)]                               # có cuộc gọi đến: nghe
    assert await _wait(lambda: hass.states.get(sensor).state == "incoming")
    hass.bus.async_fire("tele_nut")
    assert await _wait(lambda: _accepts(gw))
    assert _accepts(gw)[-1]["json"]["id"] == "n1"
    gw.calls = [dict(RINGING, id="n1", user_id=5, state="active")]               # đang nói: cúp
    assert await _wait(lambda: hass.states.get(sensor).state == "answered")
    hass.bus.async_fire("tele_nut")
    assert await _wait(lambda: [r for r in gw.requests if r["path"] == "/api/live/hangup"])
    assert [r for r in gw.requests if r["path"] == "/api/live/hangup"][-1]["json"] == {"id": "n1"}
