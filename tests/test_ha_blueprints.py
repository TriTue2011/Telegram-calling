"""Blueprint Telegram Call chạy THẬT trong HA (nạp đúng file blueprint của kho TriTue2011/Blueprint):
quy tắc cuộc gọi đến (mỗi cuộc kích hoạt automation, tự nghe máy đúng người/đúng giờ).

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
    for f in ("telegram_call_incoming_rule.yaml",):
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


def _rule(setup, alias, **inputs):
    setup.runtime_data.voice.decide_grace = 0.4                                    # chờ chốt ngắn cho test (mặc định 1 giây)
    return {"alias": alias, "use_blueprint": {"path": "TriTue2011/telegram_call_incoming_rule.yaml", "input": inputs}}


def _settings(gw):
    return [r["json"] for r in gw.requests if r["path"] == "/api/live/settings"]


async def test_incoming_rule_answers_only_the_listed_caller_per_call_with_the_chosen_devices(hass, setup, gw):
    triggered = []
    hass.bus.async_listen("automation_triggered", lambda e: triggered.append(e.data["entity_id"]))
    await _load(hass, _rule(setup, "Tele A", pham_vi="thread", them_theo_id=["5"], tu_nghe_may=True, nghe_sau=0, noi_ngay=["speaker"]))
    gw.calls = [dict(RINGING, id="x1", user_id=77)]                              # người lạ không có trong danh sách
    await asyncio.sleep(3.0)
    assert not _accepts(gw)
    gw.calls = [dict(RINGING, id="12345", user_id=5)]            # id toàn chữ số: HA đọc mẫu thành SỐ, blueprint phải ép chuỗi hai vế
    assert await _wait(lambda: _accepts(gw), 8), "không nghe máy cho người trong danh sách"
    assert _accepts(gw)[-1]["json"] == {"id": "12345"} and len(_accepts(gw)) == 1
    assert "automation.tele_a" in triggered                                      # mỗi cuộc gọi có lượt chạy automation
    held = _settings(gw)[-1]
    assert held["mic"] == "silence", held                                        # không tích «mic» thì mic im lặng, không phải âm thử


async def test_incoming_rule_waits_the_set_seconds_and_does_not_answer_a_call_that_was_dropped(hass, setup, gw):
    await _load(hass, _rule(setup, "Tele B", pham_vi="contacts", tu_nghe_may=True, nghe_sau=3))
    gw.calls = [dict(RINGING, id="y1", user_id=1)]                               # ID 1 có trong danh bạ giả của gateway
    await asyncio.sleep(2.0)
    assert not _accepts(gw), "nghe máy quá sớm (chưa hết 3 giây)"
    gw.calls = [dict(RINGING, id="y1", user_id=1, state="ended")]                  # người gọi cúp trong lúc chờ
    await asyncio.sleep(3.5)
    assert not _accepts(gw)


async def test_two_accounts_each_automation_answers_only_its_own_account(hass, setup, gw):
    """Gateway có 2 tài khoản Telegram ⇒ 2 mục. Automation gán `tai_khoan` chỉ nhận cuộc gọi của tài khoản đó, và gọi dịch vụ với ĐÚNG mục
    (không có config_entry_id thì dịch vụ báo «có nhiều tài khoản»). Sự kiện cuộc gọi mang config_entry_id + account."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from test_ha_end_to_end import PASSWORD, USER
    second = MockConfigEntry(domain="telegram_call", data={"url": gw.url, "username": USER, "password": PASSWORD, "account": "+84900000002"}, title="Telegram Call 2")
    second.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second.entry_id)                    # HAI mục cùng gateway ⇒ dịch vụ không tự đoán được mục
    await hass.async_block_till_done()
    events = []
    hass.bus.async_listen("telegram_call_call_state", lambda e: events.append(dict(e.data)))
    await _load(hass, _rule(setup, "Tele Mot", pham_vi="thread", them_theo_id=["5"], tu_nghe_may=True, nghe_sau=0, noi_ngay=["speaker"], tai_khoan=setup.entry_id))
    await _load(hass, _rule(setup, "Tele Khac", pham_vi="thread", them_theo_id=["5"], tu_nghe_may=True, nghe_sau=0, noi_ngay=["speaker"], tai_khoan="khong-phai-muc-nay"))
    gw.calls = [dict(RINGING, id="777", user_id=5)]
    assert await _wait(lambda: _accepts(gw), 8), "không nghe máy cho cuộc gọi của tài khoản đã gán"
    await asyncio.sleep(1.5)
    assert len(_accepts(gw)) == 1, "automation gán tài khoản khác không được tranh nghe"
    ring = [e for e in events if e.get("state") == "incoming"]
    assert {e["config_entry_id"] for e in ring} == {setup.entry_id, second.entry_id}, ring                 # mỗi tài khoản bắn sự kiện kèm mã của nó
    assert {e["account"] for e in ring} == {"+84900000000", "+84900000002"}, ring


async def test_incoming_rule_off_or_disabled_does_nothing(hass, setup, gw):
    await _load(hass, _rule(setup, "Tele C", pham_vi="contacts", nghe_sau=0, bat_quy_tac=False),
                _rule(setup, "Tele D", pham_vi="contacts", nghe_sau=0))
    await hass.services.async_call("automation", "turn_off", {"entity_id": "automation.tele_d"}, blocking=True)
    gw.calls = [dict(RINGING, id="z1", user_id=1)]
    await asyncio.sleep(3.0)
    assert not _accepts(gw)


async def test_scopes_contacts_and_strangers_use_the_event_and_a_person_rule_beats_the_contacts_rule(hass, setup, gw):
    await _load(hass, _rule(setup, "Tele moi nguoi", pham_vi="contacts", nghe_sau=0),
                _rule(setup, "Tele chan", pham_vi="thread", them_theo_id=["2"], tu_nghe_may=False),
                _rule(setup, "Tele la", pham_vi="strangers", nghe_sau=0))
    gw.calls = [dict(RINGING, id="c-2", user_id=2)]                              # trong danh bạ nhưng có quy tắc riêng «không nghe»: thắng quy tắc danh bạ
    await asyncio.sleep(3.0)
    assert not _accepts(gw)
    gw.calls = [dict(RINGING, id="c-1", user_id=1)]                              # trong danh bạ, không quy tắc riêng: nghe theo quy tắc danh bạ
    assert await _wait(lambda: _accepts(gw), 8)
    assert _accepts(gw)[-1]["json"]["id"] == "c-1"
    gw.calls = [dict(RINGING, id="c-99", user_id=99)]                            # người lạ: quy tắc «người lạ»
    assert await _wait(lambda: len(_accepts(gw)) == 2, 8)
    assert _accepts(gw)[-1]["json"]["id"] == "c-99"


async def test_person_picked_from_the_notify_entities_of_the_integration(hass, setup, gw):
    eid = er.async_get(hass).async_get_entity_id("notify", DOMAIN, "%s_to_1" % setup.entry_id)
    assert eid, "chưa có thực thể notify của người trong danh bạ"
    await _load(hass, _rule(setup, "Tele E", pham_vi="thread", nguoi_cu_the=[eid], nghe_sau=0))
    gw.calls = [dict(RINGING, id="n-1", user_id=1)]
    assert await _wait(lambda: _accepts(gw), 8)
