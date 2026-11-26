"""Kiểm logic call_rules bản gộp không cần cài Home Assistant: chống đua claim khi AI đang chạy, AI_DEFAULT_OPENING, bỏ mic/loa khi ai=true, resample 48k→16k."""
import ast
import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "telegram_call"


def _load(filename, names, **namespace):
    path = ROOT / filename
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nodes = []
    for n in tree.body:
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names:
            nodes.append(n)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    nodes.append(n)
                    break
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return SimpleNamespace(**namespace)


def _voice_mod():
    async def _noop(*args, **kwargs):
        return None

    return _load(
        "call_rules.py",
        ["Claim", "CallVoice", "async_apply_claim", "on_call_state", "best_claims", "primary_claim",
         "SCOPES", "AI_DEFAULT_OPENING", "AI_STT_RATE", "_Resampler"],
        asyncio=asyncio,
        contextlib=contextlib,
        dataclass=dataclass,
        field=field,
        _LOGGER=logging.getLogger("test_call_rules_ai"),
        SCOPES=("thread", "tag", "contacts", "strangers"),
        HomeAssistant=object,
        Any=object,
        _connect_by_rule=_noop,
        _run_greeting=_noop,
        _execute_rules=lambda *args, **kwargs: None,
        _begin_decide=lambda *args, **kwargs: None,
        _stop_announce=lambda *args, **kwargs: None,
        async_handle_video_request=_noop,
    )


def test_late_claim_after_answered_does_not_cancel_running_ai_task():
    mod = _voice_mod()
    scheduled_coros = []

    def fake_create_task(coro):
        scheduled_coros.append(coro)
        coro.close()
        return SimpleNamespace(cancelled=lambda: False)

    async def scenario():
        ai_task = asyncio.create_task(asyncio.sleep(10))
        try:
            voice = mod.CallVoice()
            voice.answered_ids.add("c1")
            voice.tasks["c1"] = ai_task
            voice.rule_of["c1"] = {"answer": True, "ai": True}
            rt = SimpleNamespace(voice=voice)
            hass = SimpleNamespace(async_create_task=fake_create_task)
            status = await mod.async_apply_claim(
                hass, rt, "c1", mod.Claim({"answer": False, "connect_now": ["mic"]}, rank=2, source="automation.cham")
            )
            assert status == "late"
            assert not scheduled_coros, "không được lên lịch _connect_by_rule đè lên tác vụ AI đang chạy"
            assert not ai_task.cancelled() and voice.tasks.get("c1") is ai_task
        finally:
            ai_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ai_task

    asyncio.run(scenario())


def test_late_claim_after_answered_while_greeting_pending_returns_late():
    mod = _voice_mod()
    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return object()

    async def scenario():
        voice = mod.CallVoice()
        voice.answered_ids.add("c1")
        voice.greeting_for["c1"] = {"answer": True, "ai": True}
        rt = SimpleNamespace(voice=voice)
        hass = SimpleNamespace(async_create_task=fake_create_task)
        status = await mod.async_apply_claim(hass, rt, "c1", mod.Claim({"answer": True}, rank=0, source="automation.tre"))
        assert status == "late"
        assert not scheduled
        assert "c1" in voice.greeting_for

    asyncio.run(scenario())


def test_async_run_ai_speaks_default_opening_when_greeting_empty():
    said = []
    muted_log = []

    class FakeListener:
        def __init__(self, hass, url):
            self.url = url
            self.muted = False
            self.ended = False

        def start(self):
            pass

        def drain(self):
            muted_log.append(("drain", self.muted))

        def stop(self):
            self.ended = True

    async def fake_say(hass, rt, call_id, message, engine, language, options):
        muted_log.append(("say", FakeListener_last.muted))
        said.append(message)

    async def fake_one_turn(*args, **kwargs):
        return "", "", None

    FakeListener_last = None

    def make_listener(hass, url):
        nonlocal FakeListener_last
        FakeListener_last = FakeListener(hass, url)
        return FakeListener_last

    mod = _load(
        "call_rules.py",
        ["AI_DEFAULT_OPENING", "AI_MAX_TURNS", "AI_MAX_CALL_S", "AI_IDLE_TURNS", "AI_END_RE",
         "ai_phone_hint", "AI_PHONE_HINT", "AI_PUBLIC_HINT", "AI_END_HINT", "async_run_ai"],
        asyncio=asyncio,
        re=re,
        time=time,
        _LOGGER=logging.getLogger("test_call_rules_ai"),
        HomeAssistant=object,
        Any=object,
        Context=lambda: SimpleNamespace(),
        TelegramCallError=RuntimeError,
        _AiListener=make_listener,
        _ai_one_turn=fake_one_turn,
        async_say=fake_say,
    )

    async def live_media(call_id, action=None):
        # Gateway Telegram trả token gộp trong url, không có khoá "token" riêng.
        return {"audio_out": {"url": f"/api/live/audio/out/{call_id}?t=tok"}}

    rt = SimpleNamespace(api=SimpleNamespace(url="http://gw.local", live_media=live_media))
    hass = SimpleNamespace(config=SimpleNamespace(language="vi"))
    res = asyncio.run(mod.async_run_ai(hass, rt, "c1", {"ai": True, "greeting": ""}, "tts.a", "vi", None))
    assert said == [mod.AI_DEFAULT_OPENING]
    assert muted_log == [("say", True), ("drain", True)]
    assert FakeListener_last.muted is False and FakeListener_last.ended is True
    assert res == {"finished": False, "heard": ""}


def test_answer_by_rule_drops_mic_and_speaker_when_ai_enabled():
    overrides = []
    accepts = []

    async def refresh():
        return None

    async def apply_override(call_id, override, held=False, echo_mode=None):
        overrides.append((call_id, override, held))

    async def accept(call_id):
        accepts.append(call_id)

    rules_mod = _load("call_rules.py", ["rule_override", "_PART_KEYS", "_still_ringing", "_answer_by_rule"],
                      asyncio=asyncio, contextlib=contextlib,
                      _LOGGER=logging.getLogger("test_call_rules_ai"),
                      HomeAssistant=object, Any=object, CallVoice=object,
                      TelegramCallError=RuntimeError, ANNOUNCE_WAIT_MAX=1.0)
    voice = SimpleNamespace(announce_done={}, greeting_for={})
    rt = SimpleNamespace(
        voice=voice,
        coordinator=SimpleNamespace(
            async_refresh=refresh,
            live_calls=lambda: [{"id": "c1", "state": "ringing", "outgoing": False}],
        ),
        bridge=SimpleNamespace(async_apply_override=apply_override),
        api=SimpleNamespace(accept=accept),
    )
    rule = {"answer": True, "ai": True, "connect_now": ["mic", "speaker", "camera"],
            "mic_url": "http://r1.local/mic.pcm", "speaker": "media_player.r1", "camera": "camera.cua"}
    asyncio.run(rules_mod._answer_by_rule(object(), rt, "c1", rule))
    assert accepts == ["c1"]
    assert len(overrides) == 1
    override = overrides[0][1]
    assert override["mic"] == "" and override["mic_url"] == "" and override["speaker"] == ""
    assert override["camera"] == "camera.cua"
    assert voice.greeting_for["c1"] is rule


def test_resampler_downsamples_48khz_to_16khz():
    mod = _voice_mod()
    resampler = mod._Resampler(48000)
    # 6 mẫu PCM16 ở 48 kHz -> trung bình mỗi 3 mẫu ra 2 mẫu 16 kHz.
    pcm48 = b"".join(int(v).to_bytes(2, "little", signed=True) for v in (300, 300, 300, 600, 600, 600))
    pcm16 = resampler(pcm48)
    out = [int.from_bytes(pcm16[i:i + 2], "little", signed=True) for i in range(0, len(pcm16), 2)]
    assert out == [300, 600]
