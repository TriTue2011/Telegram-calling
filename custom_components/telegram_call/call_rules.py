"""Chạy QUY TẮC cho cuộc gọi Telegram: mỗi cuộc gọi ĐẾN kích hoạt automation, automation gọi ``telegram_call.apply_call_rule`` (một «claim»);
tích hợp chờ ``DECIDE_GRACE`` giây cho các automation báo về, chọn quy tắc CỤ THỂ NHẤT rồi: báo ra loa (mọi quy tắc cùng bậc), nghe máy theo quy tắc làm chính
(thiết bị nhà, lời nhắn), và xử lý «người nhà nói trực tiếp» / tắt mic / tắt hình / đọc lời vào cuộc gọi.

Cùng thiết kế với ``zalo_call/call_voice.py`` (xem phần mô tả ở đó). Khác biệt vì gateway Telegram: không có trợ lý AI trò chuyện (gateway chưa đưa tiếng bên kia
cho HA), thiết bị nhà được đặt cho gateway TRƯỚC khi nghe máy (``bridge.async_apply_override``) rồi đổi giữa cuộc gọi bằng ``/api/live/media``.
Quy tắc lưu sẵn (``set_rule``) chỉ là bản dự phòng khi không automation nào áp quy tắc cho cuộc gọi.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .api import TelegramCallError
from .bridge_core import entity_of_label
from .rules import SCOPES, get_rules, resolve as resolve_rule

_LOGGER = logging.getLogger(__name__)

POLL_S = 0.4
DECIDE_GRACE = 1.0              # giây chờ các automation «áp quy tắc» báo về trước khi quyết; 0 = quyết ngay
ANNOUNCE_WAIT_MAX = 10.0        # giây tối đa tự nghe máy chờ câu báo tên người gọi đọc xong
MAX_ANNOUNCE_LOOPS = 20
DEFAULT_ANNOUNCE = "Có cuộc gọi {loai} từ {ten}"
SAY_MAX_S = 120.0
ALERT_APPEAR_S = 6.0           # chờ cuộc gọi đi xuất hiện trong danh sách của gateway
ALERT_WAIT_ANSWER_S = 60.0     # cuộc gọi cảnh báo không ai nghe sau chừng này giây thì cúp


@dataclass
class Claim:
    """Một quy tắc do automation áp cho MỘT cuộc gọi (dịch vụ apply_call_rule). rank = độ cụ thể của phạm vi (số nhỏ = cụ thể hơn)."""
    rule: dict[str, Any]
    rank: int
    source: str = ""         # automation đã áp (entity_id): chống áp lặp, ghi rõ ai thắng trong nhật ký
    seq: int = 0             # thứ tự tới (cùng độ cụ thể thì cái tới trước làm chính)


def best_claims(claims: list[Claim]) -> list[Claim]:
    """Chỉ giữ các quy tắc CỤ THỂ NHẤT (cùng bậc): quy tắc theo người thay hẳn quy tắc theo thẻ/danh bạ."""
    if not claims:
        return []
    top = min(c.rank for c in claims)
    return sorted((c for c in claims if c.rank == top), key=lambda c: c.seq)


def primary_claim(claims: list[Claim]) -> Claim | None:
    """Quy tắc LÀM CHÍNH (nghe máy, thiết bị, lời nhắn): quy tắc đầu tiên có `answer`, không có thì quy tắc đầu tiên. Báo ra loa thì mọi quy tắc đều làm."""
    top = best_claims(claims)
    return next((c for c in top if c.rule.get("answer")), top[0] if top else None)


@dataclass
class CallVoice:
    incoming: set[str] = field(default_factory=set)
    answered_ids: set[str] = field(default_factory=set)
    announce_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    announce_done: dict[str, asyncio.Event] = field(default_factory=dict)
    announced: set[str] = field(default_factory=set)
    scheduled: set[str] = field(default_factory=set)
    timers: dict[str, Any] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    claims: dict[str, list[Claim]] = field(default_factory=dict)
    decided: dict[str, list[Claim]] = field(default_factory=dict)
    rule_of: dict[str, dict[str, Any]] = field(default_factory=dict)
    call_payload: dict[str, dict[str, Any]] = field(default_factory=dict)
    decide_timers: dict[str, Any] = field(default_factory=dict)
    greeting_for: dict[str, dict[str, Any]] = field(default_factory=dict)       # call_id -> quy tắc có lời nhắn, chạy khi «đã nghe máy»
    connected: set[str] = field(default_factory=set)                            # call_id đã nối thiết bị theo quy tắc sau khi có người nghe tay (mỗi cuộc một lần)
    decide_grace: float | None = None                                           # None = DECIDE_GRACE
    claim_seq: int = 0

    def cancel_all(self) -> None:
        for task in list(self.tasks.values()) + list(self.announce_tasks.values()):
            task.cancel()
        self.tasks.clear()
        self.announce_tasks.clear()
        for cancel in list(self.timers.values()) + list(self.decide_timers.values()):
            cancel()
        self.timers.clear()
        self.decide_timers.clear()


# -- giọng đọc ---------------------------------------------------------------------------------------------------------

def pipeline_tts(hass: HomeAssistant, pipeline_id: str | None) -> tuple[str | None, str | None, dict | None]:
    """(thực thể TTS, ngôn ngữ, tuỳ chọn giọng) của một Assist pipeline (None = pipeline ưu tiên). Không lấy được thì (None, None, None)."""
    from homeassistant.components.assist_pipeline import pipeline as pipeline_mod
    try:
        pl = pipeline_mod.async_get_pipeline(hass, pipeline_id)
    except Exception:  # noqa: BLE001 - không có pipeline: dùng TTS mặc định của HA
        return None, None, None
    return pl.tts_engine, pl.tts_language, ({"voice": pl.tts_voice} if pl.tts_voice else None)


async def async_tts_audio(hass: HomeAssistant, message: str, engine: str | None = None, language: str | None = None,
                          options: dict | None = None) -> tuple[str, bytes]:
    """(đuôi tệp, byte) của giọng đọc `message` bằng TTS của HA."""
    from homeassistant.components import tts
    media_id = tts.generate_media_source_id(hass, message, engine=engine or None, language=language or None, options=options, cache=False)
    return await tts.async_get_media_source_audio(hass, media_id)


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def spoken_name(rule: dict[str, Any], payload: dict[str, Any]) -> str:
    """Tên để ĐỌC: tên trong quy tắc → tên danh bạ Telegram → «người lạ»/«một người». Không bao giờ đọc dãy số (ID)."""
    for cand in (rule.get("name"), payload.get("name")):
        name = str(cand or "").strip()
        if name and not name.isdigit():
            return name
    return "người lạ" if payload.get("contact_status") == "stranger" else "một người"


def announce_text(rule: dict[str, Any], payload: dict[str, Any]) -> str:
    kind = "video" if payload.get("video") else "thoại"
    return str(rule.get("announce_text") or DEFAULT_ANNOUNCE).format_map(_Safe(loai=kind, ten=spoken_name(rule, payload)))


async def async_say(hass: HomeAssistant, rt: Any, call_id: str, message: str, engine: str | None = None, language: str | None = None,
                    options: dict | None = None, wait: bool = True) -> float:
    """Đọc `message` VÀO cuộc gọi đang nói: giọng của HA (TTS theo pipeline/engine); HA không đọc được thì giọng của gateway. Trả số giây; `wait` = chờ đọc xong."""
    message = (message or "").strip()
    if not message:
        return 0.0
    try:
        _ext, audio = await async_tts_audio(hass, message, engine, language, options)
    except Exception:  # noqa: BLE001 - HA chưa có TTS: gateway tự đọc
        _LOGGER.debug("HA không tạo được giọng đọc, dùng TTS của gateway", exc_info=True)
        audio = None
    res = await rt.api.live_say(call_id, message if audio is None else "", audio=audio)
    seconds = float((res or {}).get("seconds") or 0)
    if wait:
        await asyncio.sleep(min(SAY_MAX_S, seconds + 0.6))
    return seconds


# -- thiết bị nhà ------------------------------------------------------------------------------------------------------

_PART_KEYS = {"mic": ("mic", "mic_url"), "speaker": ("speaker", "volume"), "camera": ("camera",)}


def rule_override(rule: dict[str, Any], parts: list[str]) -> dict[str, Any]:
    """Thiết bị của quy tắc để nối: phần ĐÃ TÍCH dùng thiết bị ghi trong quy tắc (quy tắc không ghi thì dùng mặc định của tích hợp); phần KHÔNG tích được ghi đè
    rỗng tường minh (= không nối, KHÔNG rơi về thiết bị mặc định)."""
    out: dict[str, Any] = {"mic": "", "mic_url": "", "speaker": "", "camera": ""}
    for part in parts:
        keys = [k for k in _PART_KEYS[part] if k != "volume"]
        chosen = {k: rule[k] for k in keys if rule.get(k)}
        for k in keys:
            out.pop(k, None)
        out.update(chosen)
        if part == "speaker" and rule.get("volume"):
            out["volume"] = rule["volume"]
    return out


def rule_parts(rule: dict[str, Any]) -> list[str]:
    """Mọi phần nối khi người nhà nói trực tiếp: connect + connect_now."""
    both = set(rule.get("connect") or []) | set(rule.get("connect_now") or [])
    return [p for p in ("mic", "speaker", "camera") if p in both]


# -- tra quy tắc -------------------------------------------------------------------------------------------------------

def rule_for_call(rt: Any, payload: dict[str, Any]) -> dict[str, Any] | None:
    """Quy tắc của MỘT cuộc gọi — chỗ DUY NHẤT trả lời «cuộc này làm theo quy tắc nào»: đã chốt → quy tắc automation đã áp → quy tắc lưu sẵn (dự phòng)."""
    voice: CallVoice = rt.voice
    cid = str(payload.get("call_id"))
    chosen = voice.rule_of.get(cid)
    if chosen is not None:
        return chosen
    main = primary_claim(voice.claims.get(cid, []))
    if main is not None:
        return main.rule
    return resolve_rule(get_rules(rt.defaults), payload.get("user_id"), payload.get("contact_status"))


# -- báo ra loa --------------------------------------------------------------------------------------------------------

def announce_targets(rule: dict[str, Any]) -> list[str]:
    """Các loa báo cuộc gọi đến: `announce_to` có thể là NHIỀU loa cách nhau dấu phẩy: báo ra TẤT CẢ."""
    return [t for t in (entity_of_label(c.strip()) for c in str(rule.get("announce_to") or "").split(",")) if t]


async def _wait_player_idle(hass: HomeAssistant, entity_id: str, start_timeout: float = 3.0, end_timeout: float = 45.0) -> None:
    loop = asyncio.get_running_loop()
    busy = ("playing", "buffering")
    t = loop.time()
    while loop.time() - t < start_timeout and getattr(hass.states.get(entity_id), "state", None) not in busy:
        await asyncio.sleep(POLL_S)
    t = loop.time()
    while loop.time() - t < end_timeout and getattr(hass.states.get(entity_id), "state", None) in busy:
        await asyncio.sleep(POLL_S)


async def _announce_once(hass: HomeAssistant, rule: dict[str, Any], payload: dict[str, Any]) -> bool:
    engine, language, options = pipeline_tts(hass, rule.get("pipeline") or None)
    if not engine:
        _LOGGER.warning("Thông báo cuộc gọi đến: chưa có giọng đọc (TTS) - chọn pipeline trong quy tắc")
        return False
    targets = announce_targets(rule)
    data: dict[str, Any] = {"entity_id": engine, "media_player_entity_id": targets if len(targets) > 1 else (targets[0] if targets else rule["announce_to"]),
                            "message": announce_text(rule, payload)}
    if language:
        data["language"] = language
    if options:
        data["options"] = options
    try:
        await hass.services.async_call("tts", "speak", data, blocking=False)
    except Exception:  # noqa: BLE001 - lỗi loa/TTS không được làm hỏng việc nghe máy
        _LOGGER.warning("Không phát được thông báo cuộc gọi đến ra %s", rule["announce_to"], exc_info=True)
        return False
    return True


async def _announce_incoming(hass: HomeAssistant, rt: Any, payload: dict[str, Any], ev: asyncio.Event | None, rule: dict[str, Any] | None) -> bool:
    """Quy tắc bật `announce` (và chọn loa `announce_to`) thì đọc thông báo ra loa; có `announce_repeat` (giây) thì báo lại tới khi nghe máy/kết thúc."""
    if not rule or not rule.get("announce") or not rule.get("announce_to"):
        return False
    cid = str(payload.get("call_id"))
    players = announce_targets(rule)
    old_levels: dict[str, Any] = {}
    level = int(rule.get("announce_volume") or 0)
    if level > 0:                                                 # âm lượng riêng cho tiếng báo; nhớ mức cũ để trả lại nếu không ai nghe
        for player in players:
            prev = getattr(hass.states.get(player), "attributes", {}).get("volume_level")
            try:
                await hass.services.async_call("media_player", "volume_set", {"entity_id": player, "volume_level": level / 100}, blocking=True)
                if prev is not None:
                    old_levels[player] = prev
            except Exception:  # noqa: BLE001
                _LOGGER.warning("Không đặt được âm lượng báo cuộc gọi cho %s", player, exc_info=True)

    async def _wait_all() -> None:
        await asyncio.gather(*(_wait_player_idle(hass, p) for p in players))
    try:
        if not await _announce_once(hass, rule, payload):
            return False

        async def _mark_first_done() -> None:                     # tự nghe máy chỉ chờ câu ĐẦU
            try:
                await _wait_all()
            finally:
                if ev is not None:
                    ev.set()
        hass.async_create_task(_mark_first_done())
        gap = int(rule.get("announce_repeat") or 0)
        for _ in range(MAX_ANNOUNCE_LOOPS if gap > 0 else 0):
            await _wait_all()
            await asyncio.sleep(gap)
            if cid not in rt.voice.incoming:
                break
            if not await _announce_once(hass, rule, payload):
                break
        else:
            if gap <= 0:
                await _wait_all()
        return True
    finally:
        if old_levels and cid not in rt.voice.answered_ids:        # không ai nghe: trả loa về mức cũ
            for player, prev in old_levels.items():
                with contextlib.suppress(Exception):
                    await hass.services.async_call("media_player", "volume_set", {"entity_id": player, "volume_level": prev}, blocking=True)


async def async_announce_incoming(hass: HomeAssistant, rt: Any, payload: dict[str, Any], rules: list[dict[str, Any] | None]) -> bool:
    """Báo tên người gọi ra loa theo MỌI quy tắc cùng bậc (mỗi automation báo ra loa của riêng nó). Luôn đánh dấu «xong» khi thoát để tự nghe máy không chờ vô ích."""
    ev = rt.voice.announce_done.get(str(payload.get("call_id")))
    try:
        results = await asyncio.gather(*(_announce_incoming(hass, rt, payload, ev, r) for r in rules if r))
        return any(results)
    finally:
        if ev is not None:
            ev.set()


# -- nghe máy theo quy tắc ---------------------------------------------------------------------------------------------

def _still_ringing(rt: Any, call_id: str) -> bool:
    return any(str(c.get("id")) == call_id and c.get("state") == "ringing" and not c.get("outgoing") for c in rt.coordinator.live_calls())


async def _answer_by_rule(hass: HomeAssistant, rt: Any, call_id: str, rule: dict[str, Any]) -> None:
    """Hết giờ chờ: nếu cuộc gọi còn đang đổ chuông (người gọi có thể đã cúp) thì nghe máy theo quy tắc: chỉ nối thiết bị `connect_now`, phần còn lại im lặng/hình đen."""
    voice: CallVoice = rt.voice
    try:
        ev = voice.announce_done.get(call_id)
        if ev is not None and not ev.is_set():                    # báo tên xong rồi mới nghe máy (không đè lên cuộc gọi)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), timeout=ANNOUNCE_WAIT_MAX)
        await rt.coordinator.async_refresh()
        if not _still_ringing(rt, call_id):
            return
        await rt.bridge.async_apply_override(call_id, rule_override(rule, list(rule.get("connect_now") or [])), held=True)
        if rule.get("greeting"):
            voice.greeting_for[call_id] = rule
        try:
            await rt.api.accept(call_id)
        except TelegramCallError:
            voice.greeting_for.pop(call_id, None)
            raise
        await rt.coordinator.async_refresh()
    except asyncio.CancelledError:
        raise
    except TelegramCallError as err:
        _LOGGER.warning("Nghe máy theo quy tắc cuộc gọi %s lỗi: %s", call_id, err)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Nghe máy theo quy tắc cuộc gọi %s lỗi", call_id)


def schedule_answer(hass: HomeAssistant, rt: Any, payload: dict[str, Any], rule: dict[str, Any] | None) -> bool:
    """Cuộc gọi ĐẾN: quy tắc `answer` và cho phép loại cuộc gọi này → lên lịch nghe máy sau `answer_delay` giây. True nếu đã lên lịch."""
    cid = str(payload.get("call_id"))
    if not rule or not rule.get("answer"):
        return False
    ctype = "video" if payload.get("video") else "voice"
    if ctype not in rule.get("call_types", ()):
        return False
    voice: CallVoice = rt.voice
    if cid in voice.scheduled:
        return False
    voice.scheduled.add(cid)

    async def _go(_now: Any) -> None:
        voice.timers.pop(cid, None)
        await _answer_by_rule(hass, rt, cid, rule)

    voice.timers[cid] = async_call_later(hass, int(rule.get("answer_delay", 0)), _go)
    return True


async def _run_greeting(hass: HomeAssistant, rt: Any, call_id: str, rule: dict[str, Any]) -> None:
    """Đã nghe máy theo quy tắc có lời nhắn: đọc lời nhắn vào cuộc gọi; `hangup_after` thì đọc xong cúp máy."""
    try:
        engine, language, options = pipeline_tts(hass, rule.get("pipeline") or None)
        await asyncio.sleep(0.5)                                 # media vừa nối: chờ một nhịp để người gọi không mất đầu câu
        await async_say(hass, rt, call_id, str(rule["greeting"]), engine, language, options)
        if rule.get("hangup_after"):
            await rt.api.hangup(call_id)
            await rt.coordinator.async_refresh()
    except asyncio.CancelledError:
        raise
    except TelegramCallError as err:
        _LOGGER.warning("Đọc lời nhắn trong cuộc gọi %s lỗi: %s", call_id, err)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Đọc lời nhắn trong cuộc gọi %s lỗi", call_id)


# -- chốt quy tắc ------------------------------------------------------------------------------------------------------

def _execute_rules(hass: HomeAssistant, rt: Any, cid: str, payload: dict[str, Any], claims: list[Claim]) -> None:
    """Làm theo các quy tắc đã chốt: báo ra loa (mọi quy tắc), lên lịch nghe máy (quy tắc làm chính)."""
    voice: CallVoice = rt.voice
    main = primary_claim(claims)
    if main is not None:
        voice.rule_of[cid] = main.rule
        if len(claims) > 1:
            _LOGGER.info("Cuộc gọi %s khớp %d quy tắc cùng bậc (%s): %s làm chính (nghe máy, thiết bị); báo ra loa theo từng quy tắc",
                         cid, len(claims), ", ".join(c.source or "?" for c in claims), main.source or "?")
    voice.announce_tasks[cid] = hass.async_create_task(async_announce_incoming(hass, rt, payload, [c.rule for c in claims]))
    schedule_answer(hass, rt, payload, main.rule if main is not None else None)


def _decide(hass: HomeAssistant, rt: Any, cid: str) -> None:
    """Hết giờ chờ automation: chốt quy tắc cho cuộc gọi đến. Có quy tắc do automation áp thì chỉ dùng chúng (cụ thể nhất); không có thì dùng quy tắc lưu sẵn
    (set_rule). Không có gì = chỉ đổ chuông."""
    voice: CallVoice = rt.voice
    voice.decide_timers.pop(cid, None)
    if cid in voice.decided or cid not in voice.incoming:
        return
    payload = voice.call_payload.get(cid) or {"call_id": cid}
    claims = best_claims(voice.claims.get(cid, []))
    if not claims:
        stored = resolve_rule(get_rules(rt.defaults), payload.get("user_id"), payload.get("contact_status"))
        claims = [Claim(stored, len(SCOPES), "kho quy tắc (set_rule)")] if stored else []
    voice.decided[cid] = claims
    _execute_rules(hass, rt, cid, payload, claims)


def _begin_decide(hass: HomeAssistant, rt: Any, cid: str, payload: dict[str, Any]) -> None:
    voice: CallVoice = rt.voice
    voice.incoming.add(cid)
    if cid in voice.announced:
        return
    voice.announced.add(cid)
    voice.announce_done[cid] = asyncio.Event()
    voice.call_payload[cid] = payload
    grace = DECIDE_GRACE if voice.decide_grace is None else voice.decide_grace
    if grace <= 0:
        _decide(hass, rt, cid)
        return

    @callback
    def _fire(_now: Any) -> None:                               # @callback: chạy trong vòng lặp sự kiện (hàm thường bị HA đẩy sang luồng khác)
        _decide(hass, rt, cid)
    voice.decide_timers[cid] = async_call_later(hass, grace, _fire)


async def async_apply_claim(hass: HomeAssistant, rt: Any, call_id: Any, claim: Claim) -> str:
    """Dịch vụ apply_call_rule: một automation áp quy tắc của nó cho cuộc gọi `call_id`. Trả về: queued (chờ chốt), applied (đã chốt mà chưa có quy tắc nào nên làm ngay),
    late (đến sau khi đã chốt bằng quy tắc khác: bỏ, có ghi nhật ký), duplicate (automation này đã áp cho cuộc gọi này), recorded (đến trước khi cuộc gọi được đánh dấu «đến»),
    connected (cuộc gọi đã có người nghe: nối thiết bị của quy tắc ngay)."""
    voice: CallVoice = rt.voice
    cid = str(call_id)
    if claim.source and any(c.source == claim.source for c in voice.claims.get(cid, [])):
        return "duplicate"
    voice.claim_seq += 1
    claim.seq = voice.claim_seq
    voice.claims.setdefault(cid, []).append(claim)
    if cid in voice.answered_ids:                                # cuộc gọi đã có người nghe (tay hoặc quy tắc khác) rồi quy tắc này mới tới: nối thiết bị ngay
        payload = voice.call_payload.get(cid) or {"call_id": cid}
        if cid not in voice.rule_of:                              # quy tắc này thành quy tắc của cuộc gọi (cho take_over, đọc ở rule_for_call)
            voice.rule_of[cid] = claim.rule
        hass.async_create_task(_connect_by_rule(hass, rt, cid, payload))
        return "connected"
    if cid in voice.decide_timers:
        return "queued"
    if cid in voice.decided:
        if voice.decided[cid]:
            _LOGGER.warning("Quy tắc của %s đến sau khi cuộc gọi %s đã chốt theo %s: bỏ qua (nới thời gian chờ nếu automation chậm)",
                            claim.source or "?", cid, voice.decided[cid][0].source or "?")
            return "late"
        voice.decided[cid] = [claim]
        _execute_rules(hass, rt, cid, voice.call_payload.get(cid) or {"call_id": cid}, [claim])
        return "applied"
    return "recorded"


# -- nghe máy bằng TAY (nút R1, answer_call không kèm thiết bị...) --------------------------------------------------------

async def _connect_by_rule(hass: HomeAssistant, rt: Any, cid: str, payload: dict[str, Any]) -> None:
    """Cuộc gọi ĐẾN đã có người nghe mà HA chưa nối thiết bị nào (không phải nghe theo quy tắc, cũng không phải ``answer_call`` kèm thiết bị riêng): nối mic / loa /
    camera của QUY TẮC của người gọi — người nghe tay chính là người nói nên lấy cả «nối ngay» lẫn «khi chuyển máy» (như ``outgoing_devices`` của zalo_call).
    Không có quy tắc, hoặc quy tắc không chọn thiết bị nào: không làm gì. Mỗi cuộc một lần; lỗi chỉ ghi nhật ký (không làm hỏng cuộc gọi)."""
    voice: CallVoice = rt.voice
    if payload.get("outgoing") or cid in voice.connected or rt.bridge.has_devices(cid):
        return
    rule = rule_for_call(rt, payload)
    parts = rule_parts(rule) if rule else []
    if not parts:
        return
    voice.connected.add(cid)
    try:
        await async_take_over(hass, rt, cid, explicit=rule_override(rule, parts))
    except TelegramCallError as err:
        _LOGGER.warning("Cuộc gọi %s đã có người nghe nhưng chưa nối được thiết bị theo quy tắc: %s", cid, err)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Nối thiết bị theo quy tắc cho cuộc gọi %s lỗi", cid)


# -- điều khiển giữa cuộc gọi ------------------------------------------------------------------------------------------

async def _active_call_id(rt: Any, call_id: Any) -> str:
    if call_id not in (None, ""):
        return str(call_id)
    await rt.coordinator.async_refresh()
    active = [c for c in rt.coordinator.live_calls() if c.get("state") == "active"]
    if len(active) != 1:
        raise TelegramCallError("Không có đúng một cuộc gọi đang nói chuyện (hãy nêu call_id)")
    return str(active[0]["id"])


async def async_take_over(hass: HomeAssistant, rt: Any, call_id: Any = None, explicit: dict[str, Any] | None = None) -> dict[str, Any]:
    """Người nhà nói trực tiếp: dừng lời nhắn đang chạy, nối mic / loa / camera của QUY TẮC (connect + connect_now); cuộc gọi không có quy tắc thì dùng thiết bị mặc định
    của tích hợp. Phần chưa chọn thiết bị thì báo rõ."""
    cid = await _active_call_id(rt, call_id)
    voice: CallVoice = rt.voice
    task = voice.tasks.pop(cid, None)
    if task is not None:
        task.cancel()
    voice.greeting_for.pop(cid, None)
    rule = voice.rule_of.get(cid)
    override = explicit or (rule_override(rule, rule_parts(rule)) if rule else None)
    res = await rt.bridge.async_resolve(rt.bridge.devices_for(override))
    live = next((c for c in rt.coordinator.live_calls() if str(c.get("id")) == cid), {})
    connected: list[str] = []
    if res["mic_url"]:
        await rt.api.live_media(cid, mic=res["mic_url"], mic_format=res["mic_format"])
        connected.append("mic")
    if res["speaker_url"]:
        await rt.api.live_playback(cid, "push", res["speaker_url"])
        connected.append("speaker")
    if res["camera_url"] and live.get("video"):
        await rt.api.live_media(cid, camera=res["camera_url"], camera_off=False)
        connected.append("camera")
    if not connected:
        raise TelegramCallError("Chưa chọn mic/loa/camera nào để nối (chọn ở thiết bị Telegram Call hoặc trong quy tắc)")
    return {"call_id": cid, "connected": connected}


async def async_mute(rt: Any, call_id: Any, target: str) -> dict[str, Any]:
    """Tắt mic (gửi im lặng) hoặc tắt hình (cuộc gọi video) của cuộc gọi đang nói."""
    cid = await _active_call_id(rt, call_id)
    if target == "mic":
        await rt.api.live_media(cid, mic="silence")
    else:
        await rt.api.live_media(cid, camera_off=True)
    return {"call_id": cid, "muted": target}


async def async_run_alert(hass: HomeAssistant, rt: Any, cid: str, user_id: Any, message: str, repeat: int, after: str, engine: str | None, language: str | None,
                          options: dict | None, explicit: dict[str, Any] | None) -> None:
    """Cuộc gọi CẢNH BÁO: chờ người nhận nghe máy, đọc `message` (repeat lần), rồi làm theo `after` — hangup (cúp) / connect (nối thiết bị nhà đã chọn, không có thì cúp) /
    wait (giữ máy). Báo HA bằng sự kiện ``telegram_call_alert_done`` đúng lúc đó."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + ALERT_WAIT_ANSWER_S
    seen = False
    try:
        await rt.coordinator.async_refresh()
        while True:
            call = next((c for c in rt.coordinator.live_calls() if str(c.get("id")) == cid), None)
            if call is None:
                if not seen and loop.time() - started < ALERT_APPEAR_S:      # vừa gọi đi: bộ theo dõi chưa kịp thấy cuộc gọi, đừng tưởng đã kết thúc
                    await asyncio.sleep(POLL_S)
                    continue
                return                                                 # người nhận từ chối / cuộc gọi kết thúc
            seen = True
            if call.get("state") == "active":
                break
            if loop.time() > deadline:                                 # không ai nghe: cúp, không gọi mãi
                await rt.api.hangup(cid)
                return
            await asyncio.sleep(POLL_S)
        await asyncio.sleep(0.5)
        for _ in range(max(1, repeat)):
            await async_say(hass, rt, cid, message, engine, language, options)
        has_devices = bool(explicit and any(explicit.get(k) for k in ("mic", "mic_url", "speaker", "camera")))
        after = after or ("connect" if has_devices else "hangup")
        connected: list[str] = []
        if after == "connect":
            try:
                connected = (await async_take_over(hass, rt, cid, explicit))["connected"]
            except TelegramCallError as err:
                _LOGGER.warning("Cảnh báo %s: không nối được thiết bị nhà (%s), cúp máy", cid, err)
        if after == "hangup" or (after == "connect" and not connected):
            await rt.api.hangup(cid)
            after = "hangup"
        hass.bus.async_fire("telegram_call_alert_done", {"call_id": cid, "user_id": user_id, "after": after, "connected": bool(connected),
                                                         "connected_parts": connected, "ended": after == "hangup"})
        await rt.coordinator.async_refresh()
    except asyncio.CancelledError:
        raise
    except TelegramCallError as err:
        _LOGGER.warning("Cảnh báo cuộc gọi %s lỗi: %s", cid, err)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Cảnh báo cuộc gọi %s lỗi", cid)


async def async_accept_video(hass: HomeAssistant, rt: Any, call_id: Any = None, camera: str = "") -> dict[str, Any]:
    """Đồng ý chuyển cuộc thoại đang nói sang video: bật video phía nhà. `camera` (thực thể hoặc URL) hoặc camera mặc định thì gửi hình đó; không có thì gửi hình ĐEN (đồng ý mà không đưa hình trong nhà)."""
    cid = await _active_call_id(rt, call_id)
    url = ""
    if camera:
        url = (await rt.bridge.async_camera_url(camera)) if "." in camera and "://" not in camera else camera
    if not url:
        url = (await rt.bridge.async_resolve(rt.bridge.devices_for({"camera": camera} if camera else None)))["camera_url"]
    await rt.api.live_media(cid, video=True, camera=url or "black", camera_off=False)
    return {"call_id": cid, "video": True, "camera": bool(url)}


async def async_handle_video_request(hass: HomeAssistant, rt: Any, payload: dict[str, Any]) -> None:
    """Sự kiện «video_request» (người gọi bật camera): theo `video_request` của quy tắc cuộc gọi — ask: không làm gì (chỉ có sự kiện); accept / accept_camera: đồng ý."""
    cid = str(payload.get("call_id"))
    rule = rt.voice.rule_of.get(cid) or rule_for_call(rt, payload) or {}
    mode = rule.get("video_request") or "ask"
    if mode == "ask":
        return
    try:
        await async_accept_video(hass, rt, cid, str(rule.get("camera") or "") if mode == "accept_camera" else "")
    except TelegramCallError as err:
        _LOGGER.warning("Đồng ý chuyển sang video cuộc gọi %s lỗi: %s", cid, err)
    except Exception:  # noqa: BLE001
        _LOGGER.exception("Đồng ý chuyển sang video cuộc gọi %s lỗi", cid)


# -- dọn khi trạng thái đổi --------------------------------------------------------------------------------------------

def _stop_announce(voice: CallVoice, cid: str) -> None:
    task = voice.announce_tasks.pop(cid, None)
    if task is not None and not task.done():
        task.cancel()


def on_call_state(hass: HomeAssistant, rt: Any, state: str, call_id: Any, terminal: tuple, payload: dict[str, Any] | None = None) -> None:
    """Gọi từ bộ theo dõi cuộc gọi: «incoming» → chốt quy tắc; «answered» → lời nhắn; kết thúc → dọn lịch/kế hoạch."""
    cid = str(call_id)
    voice: CallVoice = rt.voice
    if state == "incoming":
        _begin_decide(hass, rt, cid, payload or {"call_id": cid})
    elif state == "video_request":
        hass.async_create_task(async_handle_video_request(hass, rt, payload or {"call_id": cid}))
    elif state == "answered":
        voice.answered_ids.add(cid)
        _stop_announce(voice, cid)                              # đã có người nghe: thôi báo ra loa
        rule = voice.greeting_for.pop(cid, None)
        if rule is not None and cid not in voice.tasks:
            voice.tasks[cid] = hass.async_create_task(_run_greeting(hass, rt, cid, rule))
        elif payload:                                           # nghe TAY (nút R1, answer_call...): nối thiết bị theo quy tắc của người gọi
            hass.async_create_task(_connect_by_rule(hass, rt, cid, payload))
    elif state in terminal:
        _stop_announce(voice, cid)
        voice.answered_ids.discard(cid)
        for timers in (voice.timers, voice.decide_timers):
            cancel = timers.pop(cid, None)
            if cancel:
                cancel()
        for store in (voice.claims, voice.decided, voice.rule_of, voice.call_payload, voice.announce_done, voice.greeting_for):
            store.pop(cid, None)
        voice.connected.discard(cid)
        voice.scheduled.discard(cid)
        voice.incoming.discard(cid)
        voice.announced.discard(cid)
        task = voice.tasks.pop(cid, None)
        if task:
            task.cancel()
