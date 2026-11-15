"""Telegram: cuộc gọi ĐẾN kích hoạt automation kịp thời, và answer_call nhận thiết bị riêng (loa/mic/âm lượng) do automation chọn.

    python -m pytest tests/test_ha_realtime_answer.py -p pytest_homeassistant_custom_component -o asyncio_mode=auto"""
import asyncio
import time

import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")

from aiohttp import web  # noqa: E402
from homeassistant.core import callback  # noqa: E402
from test_ha_end_to_end import DOMAIN, FakeGateway, call, setup  # noqa: E402,F401

from custom_components.telegram_call import const  # noqa: E402


class CallGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    async def _any(self, request: web.Request) -> web.Response:
        if request.path == "/api/live/calls":
            return web.json_response({"calls": self.calls})
        if request.path == "/api/live/accept":
            await request.read()
            self.requests.append({"method": "POST", "path": request.path, "json": await request.json(), "query": {}, "raw": b"", "auth": None})
            return web.json_response({"ok": True, "id": "c1", "state": "connecting"})
        return await super()._any(request)


@pytest.fixture
async def gw(socket_enabled):
    g = CallGateway()
    await g.start()
    yield g
    await g.stop()


RINGING = {"id": "c1", "user_id": 5, "outgoing": False, "video": False, "state": "ringing"}


def test_idle_poll_is_fast_enough_for_an_incoming_call_automation():
    assert const.POLL_IDLE_S <= 2 and const.POLL_BUSY_S <= 1.5          # trước đây 5 và 2 giây; gateway cho 120 yêu cầu/phút


async def test_incoming_call_fires_the_event_within_the_poll_interval(hass, setup, gw):
    seen = []

    @callback
    def _on(e):
        seen.append((time.monotonic(), dict(e.data)))
    hass.bus.async_listen(const.EVENT_STATE, _on)
    t0 = time.monotonic()
    gw.calls = [dict(RINGING)]
    end = t0 + 3.5
    while not seen and time.monotonic() < end:
        await asyncio.sleep(0.05)
    assert seen, "không có sự kiện trong 3,5 giây"
    assert seen[0][1]["state"] == "incoming" and seen[0][1]["call_id"] == "c1" and seen[0][1]["user_id"] == 5
    assert seen[0][0] - t0 <= 2.2, seen[0][0] - t0


def _settings(gw):
    return [r["json"] for r in gw.requests if r["path"] == "/api/live/settings"]


async def test_answer_call_with_its_own_devices_sets_them_before_accepting_and_restores_defaults_after(hass, setup, gw):
    gw.calls = [dict(RINGING)]
    await asyncio.sleep(2.0)                                                     # coordinator đã thấy cuộc gọi đến
    before = len(_settings(gw))
    await call(hass, "answer_call", {"mic_url": "rtsp://cam.local/stream"})
    paths = [r["path"] for r in gw.requests if r["path"] in ("/api/live/settings", "/api/live/accept")]
    assert paths[-2:] == ["/api/live/settings", "/api/live/accept"], paths           # thiết bị riêng đặt TRƯỚC khi nghe máy
    mine = _settings(gw)[before:]
    assert mine and mine[0]["mic"] == "rtsp://cam.local/stream", mine
    gw.calls = [dict(RINGING, state="active")]
    await asyncio.sleep(2.0)
    gw.calls = [dict(RINGING, state="ended")]
    await asyncio.sleep(2.0)
    await hass.async_block_till_done()
    last = _settings(gw)[-1]
    assert last["mic"] == "test", last                                           # cuộc gọi hết: trả thiết bị mặc định, không rò sang cuộc sau


async def test_answer_call_without_devices_touches_nothing(hass, setup, gw):
    gw.calls = [dict(RINGING)]
    await asyncio.sleep(2.0)
    before = len(_settings(gw))
    await call(hass, "answer_call", {})
    assert len(_settings(gw)) == before
    assert [r for r in gw.requests if r["path"] == "/api/live/accept"][-1]["json"] == {"id": "c1"}


async def test_call_state_sensor_is_idle_when_nothing_is_live_and_keeps_the_last_result_in_an_attribute(hass, setup, gw):
    from homeassistant.helpers import entity_registry as er
    sensor = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "%s_call_state" % setup.entry_id)
    assert hass.states.get(sensor).state == "idle"
    gw.calls = [dict(RINGING)]
    for _ in range(80):
        await asyncio.sleep(0.05)
        if hass.states.get(sensor).state == "incoming":
            break
    assert hass.states.get(sensor).state == "incoming"
    gw.calls = [dict(RINGING, state="ended")]                                    # nhỡ: bên kia cúp khi chưa nghe
    for _ in range(80):
        await asyncio.sleep(0.05)
        if hass.states.get(sensor).state == "idle":
            break
    st = hass.states.get(sensor)
    assert st.state == "idle" and st.attributes["last_state"] == "missed", (st.state, st.attributes)


async def test_event_is_fired_after_the_sensor_shows_the_new_state(hass, setup, gw):
    """Automation bắt sự kiện rồi đọc cảm biến (blueprint tự nghe máy) phải thấy trạng thái MỚI — không phải «idle» cũ."""
    from homeassistant.helpers import entity_registry as er
    sensor = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "%s_call_state" % setup.entry_id)
    seen = []

    @callback
    def _on(e):
        seen.append((e.data["state"], hass.states.get(sensor).state))
    hass.bus.async_listen(const.EVENT_STATE, _on)
    gw.calls = [dict(RINGING)]
    for _ in range(80):
        await asyncio.sleep(0.05)
        if seen:
            break
    assert seen == [("incoming", "incoming")], seen
