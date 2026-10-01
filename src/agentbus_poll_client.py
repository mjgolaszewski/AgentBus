"""Detached, silent participation worker and disposable local delivery projection."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import urlencode

from src.agentbus_client import atomic_json
from src.agentbus_transport_client import ClientError, api, configuration

T = TypeVar("T")


def spool_path(profile_path: Path) -> Path:
    return profile_path.with_name(profile_path.name + ".poll.json")


def _spool_lock(profile_path: Path) -> Path:
    return profile_path.with_name(profile_path.name + ".poll.lock")


def _default_spool(profile: dict) -> dict:
    return {"cursor": int(profile.get("ack_cursor", 0)), "events": []}


def change_spool(profile_path: Path, profile: dict, change: Callable[[dict], T]) -> T:
    """Serialize worker and foreground changes to one disposable projection."""
    lock_path = _spool_lock(profile_path)
    if lock_path.is_symlink():
        raise ClientError("Refusing symlinked AgentBus poll lock.")
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = spool_path(profile_path)
        if path.is_symlink():
            raise ClientError("Refusing symlinked AgentBus poll state.")
        try:
            state = json.loads(path.read_text())
        except FileNotFoundError:
            state = _default_spool(profile)
        before = json.dumps(state, sort_keys=True)
        result = change(state)
        if json.dumps(state, sort_keys=True) != before:
            atomic_json(path, state)
        return result


def peek_event(profile_path: Path, profile: dict) -> dict | None:
    def priority(event: dict) -> int:
        if event["kind"] == "CONTROL":
            return 0
        if event["kind"] == "POLICY_CHANGED":
            return 1
        if event["kind"] == "ATTENTION_REQUIRED":
            return 2
        message = event["message"]
        if message.get("audience") == "direct" and message.get("kind") == "blocker":
            return 3
        if (message.get("audience") == "direct" and
                message.get("kind") in {"request", "question", "handoff"}):
            return 4
        if message.get("action_reason") == "claimed by this identity":
            return 5
        if message.get("audience") == "broadcast":
            return 6
        return 7

    def pick(state: dict) -> dict | None:
        events = sorted(state["events"], key=lambda event: (
            priority(event),
            event["message"]["cursor"] if event["kind"] == "MESSAGE" else event["id"],
        ))
        return events[0] if events else None

    return change_spool(profile_path, profile, pick)


def retire_event(profile_path: Path, profile: dict, kind: str, event_id: str) -> None:
    def retire(state: dict) -> None:
        state["events"] = [event for event in state["events"]
                           if (event["kind"], event["id"]) != (kind, event_id)]

    change_spool(profile_path, profile, retire)


def policy_values_for_revision(profile_path: Path, profile: dict, revision: str) -> dict | None:
    def find(state: dict) -> dict | None:
        return next((event["values"] for event in state["events"]
                     if event["kind"] == "POLICY_CHANGED" and event["revision"] == revision), None)

    return change_spool(profile_path, profile, find)


def _append(state: dict, event: dict) -> None:
    if not any((item["kind"], item["id"]) == (event["kind"], event["id"])
               for item in state["events"]):
        state["events"].append(event)


def _worker_lock(profile_path: Path) -> Path:
    return profile_path.with_name(profile_path.name + ".worker.lock")


def ensure_worker(profile_path: Path, values: dict[str, str]) -> None:
    """Start one worker if none holds the per-profile lifetime lock."""
    lock_path = _worker_lock(profile_path)
    if lock_path.is_symlink():
        raise ClientError("Refusing symlinked AgentBus worker lock.")
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        # The child acquires this same lock. Concurrent starters may launch a
        # second child, but only one can enter the polling loop.
        fcntl.flock(lock, fcntl.LOCK_UN)
    log_path = profile_path.with_name(profile_path.name + ".worker.log")
    if log_path.is_symlink():
        raise ClientError("Refusing symlinked AgentBus worker log.")
    with log_path.open("ab") as log:
        os.chmod(log_path, 0o600)
        subprocess.Popen(
            [sys.executable, "-m", "src.agentbus_poll_client", str(profile_path)],
            cwd=Path(__file__).resolve().parents[1], env=values,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True, close_fds=True,
        )


def _worker(profile_path: Path, values: dict[str, str]) -> None:
    interval: float | None = None
    policy: dict = {}
    next_control = 0.0
    next_inbox = 0.0
    failures = 0
    while True:
        profile = json.loads(profile_path.read_text())
        session = profile.get("participation")
        if not session or session.get("stopped"):
            return
        if not policy:
            policy = session.get("policy_values") or {}
        control_max = float(policy.get("control_check_max_seconds", 30))
        request_timeout = min(5.0, control_max / 3)
        instant = time.monotonic()
        if instant < min(next_control, next_inbox):
            time.sleep(min(1.0, next_control - instant, next_inbox - instant))
            continue
        try:
            if instant >= next_control:
                reported_backoff = min(float(policy["max_interval_seconds"]),
                                       max(float(policy["initial_interval_seconds"]),
                                           interval or float(policy["initial_interval_seconds"])))
                response = api(values, f"/v1/sessions/{session['session_id']}/check-in",
                               {"current_backoff_seconds": reported_backoff},
                               session_token=session["session_secret"],
                               timeout_seconds=request_timeout)
                if "controls" in response:
                    def record_controls(state: dict) -> None:
                        for control in response["controls"]:
                            _append(state, {"kind": "CONTROL", "id": control["control_id"],
                                            "control": control})
                    change_spool(profile_path, profile, record_controls)
                else:
                    changed_policy = response["values"] != policy
                    policy = response["values"]
                    control_max = float(policy["control_check_max_seconds"])
                    if changed_policy:
                        interval = min(float(policy["max_interval_seconds"]),
                                       max(float(policy["initial_interval_seconds"]),
                                           interval or float(policy["initial_interval_seconds"])))
                        next_inbox = min(next_inbox, time.monotonic() + interval)
                    if response["ack_required"]:
                        change_spool(profile_path, profile, lambda state: _append(state, {
                            "kind": "POLICY_CHANGED", "id": response["revision"],
                            "revision": response["revision"], "values": policy,
                            "sources": response["sources"],
                        }))
                next_control = instant + control_max
            # Policy transitions must be acknowledged before new ordinary work.
            pending_policy = peek_event(profile_path, profile)
            if pending_policy and pending_policy["kind"] in {"CONTROL", "POLICY_CHANGED"}:
                next_inbox = max(next_inbox, time.monotonic() + control_max)
            elif time.monotonic() >= next_inbox:
                queued = change_spool(profile_path, profile, lambda state: sum(
                    event["kind"] == "MESSAGE" for event in state["events"]
                ))
                if queued >= 100:
                    next_inbox = time.monotonic() + float(policy["initial_interval_seconds"])
                    continue
                def cursor_of(state: dict) -> int:
                    return int(state["cursor"])
                cursor = change_spool(profile_path, profile, cursor_of)
                page = api(values, "/v1/inbox?" + urlencode({
                    "identity": profile["identity"], "after": cursor, "limit": 100,
                }), timeout_seconds=request_timeout)
                def record_messages(state: dict) -> None:
                    for message in page["messages"]:
                        _append(state, {"kind": "MESSAGE", "id": str(message["cursor"]),
                                        "message": message})
                    state["cursor"] = max(int(state["cursor"]), int(page["next_cursor"]))
                change_spool(profile_path, profile, record_messages)
                if page["messages"]:
                    interval = float(policy["initial_interval_seconds"])
                else:
                    interval = (float(policy["initial_interval_seconds"]) if interval is None else
                                min(float(policy["max_interval_seconds"]),
                                    interval * float(policy["backoff_factor"])))
                next_inbox = time.monotonic() if page["has_more"] else time.monotonic() + interval
            if failures:
                retire_event(profile_path, profile, "ATTENTION_REQUIRED", "transport")
            failures = 0
        except (ClientError, KeyError, ValueError, OSError) as exc:
            failures += 1
            if failures == 3:
                reason = str(exc)
                change_spool(profile_path, profile, lambda state: _append(state, {
                    "kind": "ATTENTION_REQUIRED", "id": "transport", "reason": reason,
                }))
            retry = min(control_max, 2 ** min(failures, 5))
            next_control = time.monotonic() + retry
            next_inbox = time.monotonic() + retry


def main() -> int:
    profile_path = Path(sys.argv[1])
    lock_path = _worker_lock(profile_path)
    if lock_path.is_symlink():
        raise ClientError("Refusing symlinked AgentBus worker lock.")
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        _, values = configuration()
        try:
            _worker(profile_path, values)
        except Exception:
            profile = json.loads(profile_path.read_text())
            change_spool(profile_path, profile, lambda state: _append(state, {
                "kind": "ATTENTION_REQUIRED", "id": "worker-failed",
                "reason": "poll worker failed; inspect its private log",
            }))
            raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
