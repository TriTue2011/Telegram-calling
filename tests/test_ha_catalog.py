"""Dịch vụ theo danh mục (catalog.py) TRONG Home Assistant thật: mỗi dịch vụ được đăng ký, đi tới ``/api/do/<tên>`` của gateway với đúng nơi nhắm (chat_id / user_id),
ô «Chọn người từ danh bạ» điền đúng, đầu vào sai bị từ chối TRƯỚC khi tới gateway.

    python -m pytest tests/test_ha_catalog.py -p pytest_homeassistant_custom_component
"""
import pytest

pytest.importorskip("homeassistant")
pytest.importorskip("pytest_homeassistant_custom_component")
import voluptuous as vol  # noqa: E402
from test_ha_end_to_end import DOMAIN, call, gw, setup  # noqa: E402,F401

from custom_components.telegram_call import catalog  # noqa: E402


def _sample(f: catalog.F):
    return {"int": max(f.lo or 1, 1), "bool": True, "list_int": "1, 2", "list_text": "a\nb", "select": f.options[0] if f.options else "", "long": "x", "text": "x"}[f.kind]


def _minimal(spec: catalog.S) -> dict:
    data = {f.key: _sample(f) for f in spec.fields if f.required}
    if spec.target == "chat":
        data["chat_id"] = 42
    elif spec.target == "user" and "user_id" not in data:
        data["user_id"] = 7
    return data


def _do(gw, name):
    return [r for r in gw.requests if r["path"] == "/api/do/" + name]


async def test_every_catalog_service_is_registered_and_reaches_the_gateway(hass, setup, gw):
    assert set(catalog.SERVICES) <= set(hass.services.async_services()[DOMAIN])
    for name, spec in catalog.SERVICES.items():
        res = await call(hass, name, _minimal(spec))
        assert res["service"] == name, name
        sent = _do(gw, name)
        assert len(sent) == 1 and sent[0]["auth"] == "tggw_session=SESS-GIA", name
        body = sent[0]["json"]
        assert "config_entry_id" not in body and "peer" not in body, (name, body)
        if spec.target == "chat":
            assert body["chat_id"] == 42, name
        if spec.target == "user":
            assert body["user_id"] == 7, name


async def test_peer_is_resolved_for_chat_and_user_targets(hass, setup, gw):
    await call(hass, "pin_chat", {"peer": "@ben_bap"})
    assert _do(gw, "pin_chat")[-1]["json"] == {"chat_id": 6518712943, "pinned": True, "archived": False}
    await call(hass, "block_user", {"peer": "@ben_bap"})
    assert _do(gw, "block_user")[-1]["json"] == {"user_id": 6518712943}, "chat riêng có chat_id = user_id"


async def test_recipient_picker_fills_chat_or_user_and_member_services_do_not_offer_it(hass, setup, gw):
    hass.states.async_set("notify.telegram_call_dai_ca", "unknown", {"user_id": "5", "chat_id": "5", "type": "user"})
    await call(hass, "set_mute", {"recipient": "notify.telegram_call_dai_ca", "mute_for": 3600})
    assert _do(gw, "set_mute")[-1]["json"] == {"chat_id": 5, "mute_for": 3600}
    await call(hass, "block_user", {"recipient": "notify.telegram_call_dai_ca"})
    assert _do(gw, "block_user")[-1]["json"] == {"user_id": 5}
    with pytest.raises(vol.Invalid):                       # thêm thành viên: người nhận từ danh bạ KHÔNG tự thành nhóm
        await call(hass, "add_chat_member", {"recipient": "notify.telegram_call_dai_ca", "user_id": 5})
    assert not _do(gw, "add_chat_member")


async def test_bad_input_never_reaches_the_gateway(hass, setup, gw):
    bad = [("set_mute", {"chat_id": 1, "mute_for": 10 ** 9}), ("send_typing", {"chat_id": 1, "action": "bay"}), ("send_poll", {"chat_id": 1, "question": "q"}),
           ("send_poll", {"chat_id": 1, "question": "q", "options": "a\nb", "quiz_answer": 0}), ("vote_poll", {"chat_id": 1, "message_id": 0, "options": "1"}),
           ("vote_poll", {"chat_id": 1, "message_id": 5, "options": "một"}), ("add_chat_member", {"chat_id": 1}), ("block_user", {}), ("create_group", {"title": "x"}),
           ("set_auto_delete", {"chat_id": 1, "seconds": -5}), ("pin_message", {"chat_id": 1})]
    for name, data in bad:
        with pytest.raises(vol.Invalid):
            await call(hass, name, data)
    assert not [r for r in gw.requests if r["path"].startswith("/api/do/")]


async def test_values_are_converted_for_the_gateway(hass, setup, gw):
    await call(hass, "send_poll", {"chat_id": 1, "question": "Ăn gì?", "options": "Phở\n Bún \n\nCơm"})
    assert _do(gw, "send_poll")[-1]["json"]["options"] == ["Phở", "Bún", "Cơm"], "mỗi dòng một lựa chọn, bỏ dòng trống"
    await call(hass, "vote_poll", {"chat_id": 1, "message_id": 9, "options": "1, 3"})
    assert _do(gw, "vote_poll")[-1]["json"]["options"] == [1, 3]
    await call(hass, "vote_poll", {"chat_id": 1, "message_id": 9, "options": ""})
    assert _do(gw, "vote_poll")[-1]["json"]["options"] == [], "để trống = rút phiếu"
    await call(hass, "create_group", {"title": "Nhà", "user_ids": [1, 2, "3"]})
    assert _do(gw, "create_group")[-1]["json"] == {"title": "Nhà", "user_ids": [1, 2, 3]}


async def test_read_only_services_return_data_and_others_may_not(hass, setup, gw):
    for name, spec in catalog.SERVICES.items():
        if spec.response == "only":
            assert (await call(hass, name, _minimal(spec) | ({"phone": "+84900000001"} if name == "find_user" else {})))["service"] == name
