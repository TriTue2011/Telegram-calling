"""EventBridge — cầu updates MTProto → HA.

Envelope giữ nguyên từ thiết kế: {id (đơn điệu tăng), type, account,
timestamp, data}. Loại P0: message_new, call_incoming, call_ended,
account_status. P4 bổ sung message_edited, message_deleted,
contact_joined.

Đẩy chính: POST tới HA_WEBHOOK_URL kèm header X-Gateway-Token, retry
backoff lũy tiến 3 lần rồi spool. Dự phòng: spool vào /data/events +
HA poll GET /api/events?since=cursor (dedup theo event id).
Bắt kịp sau reconnect: getDifference (TDLib: cơ chế bắt kịp tương
đương) trước khi đánh dấu account online — ở khung P0, event chỉ đến
từ mock/test, chưa từ TDLib thật.
"""

import json
import os
import threading
import time
import urllib.request

from .store import EVENTS_DIR

MAX_SPOOL_FILES = 10000


class EventBridge:
    def __init__(self, data_dir: str, webhook_url: str = "",
                 webhook_token: str = "", sender=None):
        self.events_dir = os.path.join(data_dir, EVENTS_DIR)
        os.makedirs(self.events_dir, exist_ok=True)
        self.webhook_url = webhook_url
        self.webhook_token = webhook_token
        self._sender = sender  # injectable cho test (mặc định: urllib)
        self._lock = threading.Lock()
        self._cursor = 0
        self._events: list[dict] = []
        self._restore_cursor()

    def _restore_cursor(self) -> None:
        # Khôi phục cursor từ spool gần nhất (đơn điệu tăng qua restart).
        try:
            names = sorted(os.listdir(self.events_dir))
            if names:
                with open(os.path.join(self.events_dir, names[-1]),
                          encoding="utf-8") as fh:
                    last = json.load(fh)
                self._cursor = int(last["id"])
        except (OSError, ValueError, KeyError):
            self._cursor = 0

    def emit(self, type_: str, account: str, data: dict) -> dict:
        with self._lock:
            self._cursor += 1
            event = {"id": self._cursor, "type": type_, "account": account,
                     "timestamp": time.time(), "data": data}
            self._events.append(event)
            self._events = self._events[-MAX_SPOOL_FILES:]
            path = os.path.join(self.events_dir, f"{self._cursor:012d}.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(event, fh)
            # Xoay spool cũ (giữ tối đa MAX_SPOOL_FILES file).
            names = sorted(os.listdir(self.events_dir))
            for old in names[:-MAX_SPOOL_FILES]:
                try:
                    os.remove(os.path.join(self.events_dir, old))
                except OSError:
                    pass
        self._push_webhook(event)
        return event

    def _push_webhook(self, event: dict) -> None:
        if not self.webhook_url:
            return
        body = json.dumps(event).encode("utf-8")
        for attempt in range(3):
            try:
                if self._sender is not None:
                    self._sender(self.webhook_url, body,
                                 self.webhook_token)
                else:
                    req = urllib.request.Request(
                        self.webhook_url, data=body, method="POST",
                        headers={"Content-Type": "application/json",
                                 "X-Gateway-Token": self.webhook_token})
                    urllib.request.urlopen(req, timeout=10)
                return
            except Exception:
                time.sleep(2 ** attempt)
        # Hết 3 lần retry: event đã spool, HA lấy lại qua poll fallback.

    def poll(self, since: int) -> dict:
        with self._lock:
            events = [e for e in self._events if e["id"] > since]
            return {"events": events, "cursor": self._cursor}

    @property
    def cursor(self) -> int:
        return self._cursor
