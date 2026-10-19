"""Bounded process-local observations from the existing delivery task, never probes."""
from __future__ import annotations

import threading
import time


class WorkerObservations:
    """No durable authority: a completed cycle does not prove message delivery."""
    def __init__(self):
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {name: {'state': 'not_started', 'last_attempt_at': None,
                            'last_completed_at': None, 'last_error_at': None, 'error': None}
                      for name in ('task', 'delivery', 'federation')}

    def mark(self, name, state, *, completed=False, error=None):
        with self._lock:
            row = self._rows[name]
            row['state'] = state
            if state == 'running':
                row['last_attempt_at'] = time.time()
            if completed:
                row['last_completed_at'] = time.time()
                row['error'] = None
            if error is not None:
                row['last_error_at'] = time.time()
                row['error'] = error  # Callers supply fixed codes, never exception text.

    def snapshot(self):
        with self._lock:
            return {name: dict(row) for name, row in self._rows.items()}

    def finish_task(self, *, failed=False):
        """Atomically retire active cycle claims with their actual task owner."""
        with self._lock:
            instant = time.time()
            task = self._rows['task']
            task['state'] = 'failed' if failed else 'stopped'
            if failed:
                task.update(error='unexpected_task_failure', last_error_at=instant)
            for name in ('delivery','federation'):
                row = self._rows[name]
                if row['state'] == 'running':
                    row.update(state='failed' if failed else 'interrupted', last_error_at=instant,
                               error='unexpected_cycle_failure' if failed else 'cycle_interrupted')
                elif row['state'] in {'waiting','backoff','degraded'}:
                    row['state'] = 'stopped'
