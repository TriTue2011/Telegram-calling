"""CallManager — state machine cuộc gọi 1-1 cho mỗi tài khoản.

Giới hạn thiết kế (theo nguồn thứ cấp, kiểm chứng ở bước kiểm thử 6):
mỗi tài khoản chỉ 1 cuộc 1-1/lúc. State machine duy nhất/tài khoản:
idle → dialing → ringing → active → ended. Tài khoản đang ở
dialing/ringing/active thì POST /api/call khác cho cùng tài khoản trả
409 ACCOUNT_BUSY ngay (hoặc vào hàng đợi FIFO nếu queue=true).

Hàng đợi: sâu queue_depth (mặc định 10), timeout queue_timeout
(mặc định 120 s), ưu tiên alarm > normal (alarm policy đầy đủ là P5,
nhưng trường ưu tiên có sẵn từ P0 để P5 không phải đổi API).

Giá trị mặc định (mượn từ telegram_voip làm điểm xuất phát, chỉnh sau
kiểm thử thật): ring_timeout 45 s, ring_attempts 2, ring_interval 15 s,
max_call_seconds 180 s.

Khung P0: state machine là thật và test được; media engine là stub
(xem engine.py) — cuộc gọi mock không phát tiếng thật.
"""

import time
import uuid
from dataclasses import dataclass, field

from .errors import account_busy, call_not_active


@dataclass
class Call:
    call_id: str
    account: str
    peer: str
    direction: str  # outgoing | incoming
    mode: str       # tts | live
    state: str = "dialing"  # dialing | ringing | active | ended
    started_at: float = 0.0
    ended_at: float | None = None
    reason: str = ""
    tts_text: str = ""
    tts_audio_id: str = ""
    queued: bool = False
    endpoint: str = "ha"

    @property
    def duration_seconds(self) -> float:
        end = self.ended_at or time.time()
        return max(0.0, end - self.started_at)


@dataclass(order=True)
class _Queued:
    priority: int  # 0 = alarm, 1 = normal (dataclass order: nhỏ trước)
    enqueued_at: float
    payload: dict = field(compare=False)


class CallManager:
    def __init__(self, queue_depth: int = 10, queue_timeout: int = 120):
        self.queue_depth = queue_depth
        self.queue_timeout = queue_timeout
        # account -> Call đang chạy (dialing/ringing/active)
        self._active: dict[str, Call] = {}
        # account -> hàng đợi FIFO có ưu tiên
        self._queues: dict[str, list[_Queued]] = {}
        self._calls: dict[str, Call] = {}
        # Cuộc vừa được đưa lên từ hàng đợi trong lần hangup gần nhất
        # (route dùng để claim endpoint cho nó).
        self.last_promoted: Call | None = None

    def active_for(self, account: str) -> Call | None:
        return self._active.get(account)

    def start_call(self, account: str, peer: str, mode: str,
                   tts_text: str = "", queue: bool = False,
                   priority: str = "normal", endpoint: str = "ha",
                   tts_audio_id: str = "") -> Call:
        if mode not in ("tts", "live"):
            from .errors import bad_request
            raise bad_request("mode phải là 'tts' hoặc 'live'.")
        current = self._active.get(account)
        if current is not None:
            if not queue:
                raise account_busy()
            q = self._queues.setdefault(account, [])
            if len(q) >= self.queue_depth:
                raise account_busy()
            call = Call(call_id=uuid.uuid4().hex, account=account,
                        peer=peer, direction="outgoing", mode=mode,
                        state="dialing", started_at=time.time(),
                        tts_text=tts_text, tts_audio_id=tts_audio_id,
                        queued=True, endpoint=endpoint)
            q.append(_Queued(0 if priority == "alarm" else 1,
                             time.time(),
                             {"call": call}))
            q.sort()
            self._calls[call.call_id] = call
            return call
        call = Call(call_id=uuid.uuid4().hex, account=account, peer=peer,
                    direction="outgoing", mode=mode, state="dialing",
                    started_at=time.time(), tts_text=tts_text,
                    tts_audio_id=tts_audio_id, endpoint=endpoint)
        self._active[account] = call
        self._calls[call.call_id] = call
        return call

    def get(self, call_id: str) -> Call | None:
        return self._calls.get(call_id)

    def list_calls(self) -> list[Call]:
        return list(self._calls.values())

    def mark_ringing(self, call_id: str) -> Call:
        """dialing -> ringing (chuông đổ phía peer, P2: trạng thái thật
        khi TDLib báo updateCall callStatePending/cuộc đang đổ chuông)."""
        call = self._calls.get(call_id)
        if call is None or call.state == "ended":
            raise call_not_active()
        if call.state == "dialing":
            call.state = "ringing"
        return call

    def answer(self, call_id: str, mode: str | None = None) -> Call:
        call = self._calls.get(call_id)
        if call is None or call.state == "ended":
            raise call_not_active()
        call.state = "active"
        if mode:
            call.mode = mode
        return call

    def hangup(self, call_id: str, reason: str = "hangup") -> Call:
        call = self._calls.get(call_id)
        if call is None or call.state == "ended":
            raise call_not_active()
        self.last_promoted = None
        call.state = "ended"
        call.ended_at = time.time()
        call.reason = reason
        if self._active.get(call.account) is call:
            del self._active[call.account]
            self._promote_queued(call.account)
        return call

    def _promote_queued(self, account: str) -> None:
        """Đưa request đầu hàng đợi lên thành cuộc đang chạy (nếu còn hạn)."""
        q = self._queues.get(account, [])
        now = time.time()
        while q:
            item = q.pop(0)
            if now - item.enqueued_at > self.queue_timeout:
                queued_call: Call = item.payload["call"]
                queued_call.state = "ended"
                queued_call.ended_at = now
                queued_call.reason = "queue_timeout"
                continue
            call: Call = item.payload["call"]
            call.queued = False
            call.started_at = now
            self._active[account] = call
            self.last_promoted = call
            return

    def prune_expired_queue(self) -> None:
        now = time.time()
        for account, q in list(self._queues.items()):
            kept = []
            for item in q:
                if now - item.enqueued_at > self.queue_timeout:
                    call: Call = item.payload["call"]
                    call.state = "ended"
                    call.ended_at = now
                    call.reason = "queue_timeout"
                else:
                    kept.append(item)
            self._queues[account] = kept
