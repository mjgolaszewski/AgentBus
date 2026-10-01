"""Persistent per-principal send admission before the Slack side effect."""

from __future__ import annotations

import math
import sqlite3
import threading
import time


class SendRateExceeded(Exception):
    def __init__(self, retry_after: int):
        self.retry_after = retry_after
        super().__init__("send rate exceeded")


class SendRateStore:
    WINDOW_SECONDS = 60
    MAX_SENDS = 30

    def __init__(self, db: sqlite3.Connection, lock: threading.RLock):
        self._db = db
        self._lock = lock
        with lock, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS send_rate_buckets (
                    principal TEXT PRIMARY KEY,
                    window INTEGER NOT NULL,
                    used INTEGER NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS send_rate_window ON send_rate_buckets(window)")

    def reserve(self, principal: str, *, now: float | None = None) -> None:
        instant = time.time() if now is None else now
        window = int(instant // self.WINDOW_SECONDS)
        retry_after = max(1, math.ceil((window + 1) * self.WINDOW_SECONDS - instant))
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT window, used FROM send_rate_buckets WHERE principal = ?", (principal,),
            ).fetchone()
            if row is not None and row["window"] == window:
                if row["used"] >= self.MAX_SENDS:
                    raise SendRateExceeded(retry_after)
                self._db.execute(
                    "UPDATE send_rate_buckets SET used = used + 1 WHERE principal = ?",
                    (principal,),
                )
            else:
                self._db.execute(
                    "INSERT INTO send_rate_buckets(principal, window, used) VALUES (?, ?, 1) "
                    "ON CONFLICT(principal) DO UPDATE SET window = excluded.window, used = 1",
                    (principal, window),
                )
            # An unjoined caller can choose many labels; expire idle buckets.
            self._db.execute("DELETE FROM send_rate_buckets WHERE window < ?", (window - 1,))
