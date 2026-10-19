"""Bounded process-local observations from the existing delivery task, never probes."""
from __future__ import annotations

import threading
import time


class WorkerObservations:
    """No durable authority: a completed cycle does not prove message delivery."""
    def __init__(self):
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {name: {'state': 'not_started', 'last_attempt_at': None,
                            'last_success_at': None, 'last_error_at': None, 'error': None}
                      for name in ('task', 'delivery', 'federation')}

    def mark(self, name, state, *, success=False, error=None):
        with self._lock:
            row = self._rows[name]
            row['state'] = state
            if state == 'running':
                row['last_attempt_at'] = time.time()
            if success:
                row['last_success_at'] = time.time()
                row['error'] = None
            if error is not None:
                row['last_error_at'] = time.time()
                row['error'] = error  # Callers supply fixed codes, never exception text.

    def snapshot(self):
        with self._lock:
            return {name: dict(row) for name, row in self._rows.items()}
