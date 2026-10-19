"""Optional, host-owned wake adapter over AgentBus's non-destructive poll projection."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from src import agentbus_wake_inbox_client as wake_inbox
from src.agentbus_client import atomic_json, checked_profile, consumer_state_dir, identity_path, resolve_identity
from src.agentbus_codex_rpc_client import (
    CodexAppServer,
    CodexHostError,
    CodexHostRejected,
    TurnCompletionUncertain,
    TurnStartUncertain,
)
from src.agentbus_codex_wake_events_client import _candidate, _fingerprint, _prompt, controls_projected, eligible_events
from src.agentbus_codex_wake_intent_client import (
    attach_observer,
    observe_host,
    record_intent,
    record_result,
    validate_intent,
)
from src.agentbus_poll_client import ensure_worker, worker_running
from src.agentbus_scope_client import FIELDS, scope, scoped_api, verify_projection
from src.agentbus_transport_client import ClientError, api, profile_session_token

THREAD_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
HostFactory = Callable[[], CodexAppServer]


class WakeBinding(dict[str, Any]):
    """Validated private state for one stable chat's host binding."""


MAX_WAKES_PER_HOUR = 12
MIN_WAKE_INTERVAL_SECONDS = 20


def _turn_max_seconds(values: dict[str, str]) -> int:
    raw = values.get("AGENTBUS_CODEX_TURN_MAX_SECONDS", "600")
    try:
        seconds = int(raw)
    except ValueError:
        raise ClientError("AGENTBUS_CODEX_TURN_MAX_SECONDS must be 60 to 3600") from None
    if not 60 <= seconds <= 3600:
        raise ClientError("AGENTBUS_CODEX_TURN_MAX_SECONDS must be 60 to 3600")
    return seconds


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(values: dict[str, str], chat_id: str) -> Path:
    try:
        chat_id = str(uuid.UUID(chat_id))
    except ValueError:
        raise ClientError("Codex wake needs a valid stable chat UUID") from None
    directory = consumer_state_dir(values) / "codex-wake"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ClientError("Refusing unsafe Codex wake state directory")
    return directory / f"{chat_id}.json"


def _workspace_path(values: dict[str, str]) -> Path:
    directory = consumer_state_dir(values) / "codex-wake"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ClientError("Refusing unsafe Codex wake state directory")
    return directory / "workspace.json"


def workspace_status(values: dict[str, str]) -> dict:
    path = _workspace_path(values)
    if path.is_symlink():
        raise ClientError("Refusing symlinked Codex wake state")
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return {"enabled": False}
    except (OSError, ValueError):
        raise ClientError("Codex wake workspace state is unreadable") from None
    if not isinstance(state, dict) or state.get("schema_version") != 1 or not isinstance(state.get("enabled"), bool):
        raise ClientError("Codex wake workspace state has an unsupported schema")
    return {"enabled": state["enabled"], "changed_at": state.get("changed_at")}


def set_workspace_enabled(values: dict[str, str], enabled: bool) -> dict:
    path = _workspace_path(values)
    with _locked(path):
        state = {"schema_version": 1, "enabled": enabled, "actor": "local-workspace-user", "changed_at": _now()}
        atomic_json(path, state)
        return {"enabled": enabled, "changed_at": state["changed_at"]}


def _worker_path(values: dict[str, str]) -> Path:
    return _workspace_path(values).with_name("worker.json")


def _worker_pid(path: Path) -> int | None:
    from src.agentbus_client import process_identity
    try:
        record = json.loads(path.read_text())
        pid = int(record["pid"])
        identity = process_identity(pid)
        if pid > 1 and identity is not None and identity == record["start_time"]:
            return pid
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def start_worker(project: Path, values: dict[str, str]) -> dict:
    from src.agentbus_client import process_identity
    path = _worker_path(values)
    with _locked(path):
        pid = _worker_pid(path)
        if pid is not None:
            return {"worker_pid": pid, "already_running": True}
        launcher = project / "agentbus"
        if not launcher.is_file():
            raise ClientError("AgentBus launcher is missing; cannot start Codex wake worker")
        log_path = path.with_name("worker.log")
        with log_path.open("ab") as log:
            child = subprocess.Popen([sys.executable, str(launcher), "codex-wake", "watch-all"],
                                     cwd=project, env=values, stdin=subprocess.DEVNULL,
                                     stdout=log, stderr=log, start_new_session=True, umask=0o077)
        for _ in range(10):
            if child.poll() is not None:
                raise ClientError(f"Codex wake worker exited; inspect {log_path}")
            time.sleep(0.05)
        atomic_json(path, {"pid": child.pid, "start_time": process_identity(child.pid)})
        return {"worker_pid": child.pid, "already_running": False}


def stop_worker(values: dict[str, str]) -> dict:
    path = _worker_path(values)
    with _locked(path):
        pid = _worker_pid(path)
        if pid is not None:
            # Signal only the supervisor. Its active per-binding threads must
            # retain their app-server children through a terminal observation.
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            deadline = time.monotonic() + _turn_max_seconds(values) + 60
            while _worker_pid(path) == pid and time.monotonic() < deadline:
                time.sleep(0.1)
            if _worker_pid(path) == pid:
                raise ClientError("Codex wake worker is still draining active turns; inspect worker status")
        path.unlink(missing_ok=True)
        return {"worker_stopped": pid is not None}


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    lock_path = path.with_suffix(".lock")
    if lock_path.is_symlink() or path.is_symlink():
        raise ClientError("Refusing symlinked Codex wake state")
    with lock_path.open("a") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _load(path: Path) -> WakeBinding:
    if path.is_symlink():
        raise ClientError("Refusing symlinked Codex wake state")
    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        raise ClientError("Codex wake is unbound; run `agentbus codex-wake bind` first") from None
    except (OSError, ValueError):
        raise ClientError("Codex wake state is unreadable") from None
    if (not isinstance(state, dict) or state.get("schema_version") != 1 or
            not isinstance(state.get("chat_id"), str) or
            not isinstance(state.get("session_id"), str) or
            not isinstance(state.get("thread_id"), str) or
            not isinstance(state.get("binding_generation"), int) or
            not isinstance(state.get("enabled"), bool) or
            "last_attempt" not in state or "last_fingerprint" not in state):
        raise ClientError("Codex wake state has an unsupported schema")
    # Additive local-state migration preserves bindings made by the first
    # candidate without resetting their deduplication history.
    state.setdefault("wake_epoch", 0)
    state.setdefault("wake_history", [])
    state.setdefault("same_fingerprint_retries", 0)
    if (not isinstance(state["wake_epoch"], int) or
            not isinstance(state["wake_history"], list) or
            not isinstance(state["same_fingerprint_retries"], int)):
        raise ClientError("Codex wake state has invalid rate or epoch fields")
    validate_intent(state)
    return WakeBinding(state)


def local_binding_projection(values: dict[str, str], chat_id: str, session_id: str) -> dict | None:
    """Describe this host's binding; it is not a service-global assertion."""
    path = _state_path(values, chat_id)
    if not path.exists():
        return None
    state = _load(path)
    if state["chat_id"] != chat_id:
        raise ClientError("Codex wake binding does not match its chat ID")
    return {
        "scope": "this_host", "thread_id": state["thread_id"],
        "enabled": state["enabled"], "binding_generation": state["binding_generation"],
        "session_matches": state["session_id"] == session_id,
    }


def _save(path: Path, state: WakeBinding) -> None:
    atomic_json(path, dict(state))


def _profile(values: dict[str, str], identity: str, *, allow_stopped: bool = False) -> tuple[dict, dict, Path]:
    profile, _ = checked_profile(values, identity)
    session = profile.get("participation")
    if not isinstance(session, dict) or not isinstance(session.get("session_id"), str):
        raise ClientError("Join AgentBus participation before binding a Codex thread")
    if session.get("stopped") and not allow_stopped:
        raise ClientError("Stopped participation cannot be bound or awakened")
    return profile, session, _state_path(values, profile["chat_id"])


def bind(values: dict[str, str], identity: str, thread_id: str,
         host_factory: HostFactory = CodexAppServer, *, auto_enable: bool = False) -> dict:
    if not THREAD_ID.fullmatch(thread_id):
        raise ClientError("Pass an exact Codex thread ID")
    profile, session, path = _profile(values, identity)
    try:
        with host_factory() as host:
            thread = host.read_thread(thread_id)
    except CodexHostError as exc:
        raise ClientError(str(exc)) from None
    if thread.get("ephemeral") is True:
        raise ClientError("An ephemeral Codex thread cannot be a durable wake target")
    enabled = auto_enable and workspace_status(values)["enabled"]
    with _locked(path):
        prior = _load(path) if path.exists() else None
        if prior and (prior.get("last_attempt") or {}).get("state") in {"starting", "uncertain"}:
            raise ClientError("Resolve the uncertain prior wake before rebinding")
        if prior:
            expected = scope(profile)
            legacy = any(key not in prior for key in FIELDS)
            if legacy and all(key not in prior or prior[key] == expected[key] for key in FIELDS):
                attempt = prior.get("last_attempt")
                if attempt and any(key in attempt and attempt[key] != expected[key] for key in FIELDS):
                    raise ClientError("Foreign wake attempt scope; evidence retained")
                intent = prior.get("wake_intent")
                if intent and any(key in intent and intent[key] != expected[key] for key in FIELDS):
                    raise ClientError("Foreign wake intent scope; evidence retained")
                prior.update(expected)
                if attempt:
                    attempt.update(expected)
                if intent:
                    intent.update(expected)
                _save(path, prior)
            verify_projection(prior, profile)
        if prior and prior["chat_id"] == profile["chat_id"] and prior["session_id"] == session["session_id"] and prior["thread_id"] == thread_id:
            if enabled and not prior["enabled"]:
                prior["enabled"] = True
                _save(path, prior)
            return {"chat_id": prior["chat_id"], "session_id": prior["session_id"],
                    "thread_id": thread_id, "binding_generation": prior["binding_generation"],
                    "enabled": prior["enabled"]}
        state = WakeBinding({
            "schema_version": 1, **scope(profile), "chat_id": profile["chat_id"],
            "session_id": session["session_id"], "thread_id": thread_id,
            "binding_generation": (prior["binding_generation"] + 1) if prior else 1,
            "enabled": enabled, "actor": "local-workspace-user" if auto_enable else "local-host-operator", "created_at": _now(),
            "last_fingerprint": None, "last_attempt": None,
            "wake_epoch": 0, "wake_history": [], "same_fingerprint_retries": 0,
        })
        _save(path, state)
    return {"chat_id": state["chat_id"], "session_id": state["session_id"],
            "thread_id": thread_id, "binding_generation": state["binding_generation"],
            "enabled": enabled}


def enroll_current(values: dict[str, str], identity: str,
                   host_factory: HostFactory = CodexAppServer) -> dict:
    if not workspace_status(values)["enabled"]:
        return {"status": "workspace_disabled"}
    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if not THREAD_ID.fullmatch(thread_id):
        raise ClientError("Current Codex thread ID is unavailable; use an authorized host enrollment")
    return {"status": "bound", **bind(values, identity, thread_id, host_factory, auto_enable=True)}


def set_enabled(values: dict[str, str], identity: str, enabled: bool) -> dict:
    profile, session, path = _profile(values, identity, allow_stopped=not enabled)
    with _locked(path):
        state = _load(path)
        _validate_binding(state, profile, session)
        if (state.get("last_attempt") or {}).get("state") in {"starting", "uncertain"} and enabled:
            raise ClientError("Resolve the uncertain prior wake before enabling")
        state["enabled"] = enabled
        _save(path, state)
        return {"enabled": enabled, "thread_id": state["thread_id"]}


def _validate_binding(state: WakeBinding, profile: dict, session: dict) -> None:
    verify_projection(state, profile)
    attempt = state.get("last_attempt")
    if attempt and any(attempt.get(key) != state.get(key) for key in FIELDS):
        raise ClientError("Wake attempt scope differs; evidence retained")
    if (state.get("chat_id") != profile.get("chat_id") or
            state.get("session_id") != session.get("session_id") or
            state.get("thread_id") is None):
        raise ClientError("Codex wake binding is stale for this chat/session; bind again")


def _rate_result(state: WakeBinding) -> dict | None:
    now = datetime.now(timezone.utc)
    recent: list[str] = []
    for item in state["wake_history"]:
        try:
            observed = datetime.fromisoformat(item)
            age = (now - observed).total_seconds()
        except (TypeError, ValueError):
            raise ClientError("Codex wake history is invalid") from None
        if age < 0:
            raise ClientError("Codex wake history is in the future")
        if age < 3600:
            recent.append(item)
    state["wake_history"] = recent
    if len(recent) >= MAX_WAKES_PER_HOUR:
        return {"status": "wake_rate_limited", "reason": "hourly budget exhausted",
                "operator_action": "inspect pending work and the chat before re-enabling"}
    if recent:
        age = (now - datetime.fromisoformat(recent[-1])).total_seconds()
        if age < MIN_WAKE_INTERVAL_SECONDS:
            return {"status": "wake_deferred", "retry_after_seconds": round(MIN_WAKE_INTERVAL_SECONDS - age)}
    return None


def status(values: dict[str, str], identity: str) -> dict:
    """Project own service and host-local wake facts without causing a wake."""
    profile, _ = checked_profile(values, identity)
    session = profile.get("participation")
    observed_at = _now()
    if not isinstance(session, dict) or not isinstance(session.get("session_id"), str):
        return {"identity": identity, "chat_id": profile.get("chat_id"),
                "observed_at": observed_at, "registration": "not_joined",
                "wake_readiness": "ineligible",
                "reason": "not_joined", "service_presence": None}
    path = _state_path(values, profile["chat_id"])
    state = _load(path) if path.exists() else None
    binding_matches = bool(state and state.get("chat_id") == profile["chat_id"] and
                           state.get("session_id") == session["session_id"])
    workspace_enabled = workspace_status(values)["enabled"]
    profile_path = identity_path(values, identity).resolve()
    scan_state = (wake_inbox.read_state(values, profile, state["binding_generation"])
                  if binding_matches and state is not None else None)
    scan_candidates = wake_inbox.candidates(scan_state, int(profile.get("ack_cursor", 0))) if scan_state else []
    refs = (eligible_events(profile_path, profile, scan_candidates)
            if binding_matches and not session.get("stopped") else [])
    service_presence = None
    service_error = None
    token = profile_session_token(profile)
    if token and values.get("AGENTBUS_URL") and values.get("AGENTBUS_API_TOKEN"):
        try:
            service_presence = scoped_api(values, f"/v1/sessions/{session['session_id']}/self-presence", profile,
                                   session_token=token, api_call=api)
        except ClientError as exc:
            service_error = str(exc)
    else:
        service_error = "service self-presence is not configured"
    rate = _rate_result(state) if state else None
    adapter_worker_running = _worker_pid(_worker_path(values)) is not None
    participation_worker_running = worker_running(profile_path)
    reason = "ready_to_attempt"
    readiness = "eligible"
    if session.get("stopped") or (service_presence and service_presence["state"] == "stopped"):
        readiness, reason = "ineligible", "session_stopped"
    elif service_presence and (service_presence["chat_id"] != profile["chat_id"] or
                               service_presence["route"] != identity or
                               service_presence["session_id"] != session["session_id"]):
        readiness, reason = "ineligible", "service_identity_mismatch"
    elif not binding_matches:
        readiness, reason = "ineligible", "binding_missing_or_stale"
    elif not workspace_enabled:
        readiness, reason = "ineligible", "workspace_disabled"
    elif state is not None and not state["enabled"]:
        readiness, reason = "ineligible", "chat_opted_out"
    elif values.get("AGENTBUS_CODEX_HOST_MODE") != "standalone":
        readiness, reason = "ineligible", "owning_host_endpoint_unavailable"
    elif service_presence is None:
        readiness, reason = "unknown", "service_presence_unobserved"
    elif service_presence["state"] not in {"active_compliant", "control_pending"}:
        readiness, reason = "deferred", "service_session_not_ready"
    elif not controls_projected(service_presence, refs):
        readiness, reason = "deferred", "control_projection_pending"
    elif not adapter_worker_running or not participation_worker_running:
        readiness, reason = "deferred", "local_worker_not_running"
    elif state is not None and refs:
        previous = state.get("last_attempt") or {}
        fingerprint = _fingerprint(refs)
        if previous.get("state") in {"starting", "uncertain"}:
            readiness, reason = "deferred", "wake_attempt_unresolved"
        elif previous.get("state") == "rejected" and previous.get("fingerprint") == fingerprint:
            readiness, reason = "ineligible", "prior_turn_rejected"
        elif state.get("last_fingerprint") == fingerprint:
            if state["same_fingerprint_retries"] >= 1:
                readiness, reason = "deferred", "pending_after_followup"
            elif rate and not refs[0].startswith(("CONTROL:", "POLICY_CHANGED:")):
                readiness, reason = "deferred", rate["status"]
            elif previous.get("state") in {"completed", "interrupted", "failed"}:
                readiness, reason = "eligible", "terminal_turn_has_unacknowledged_work"
            else:
                readiness, reason = "unknown", "prior_turn_completion_unobserved"
        elif rate and not refs[0].startswith(("CONTROL:", "POLICY_CHANGED:")):
            readiness, reason = "deferred", rate["status"]
    elif rate:
        readiness, reason = "deferred", rate["status"]
    last_status = (state.get("last_attempt") or {}).get("turn_status") if state else None
    return {"identity": identity, "chat_id": profile["chat_id"],
            "session_id": session["session_id"], "thread_id": state.get("thread_id") if state else None,
            "registration": "joined" if service_presence else "unverified",
            "service_state": service_presence["state"] if service_presence else None,
            "route_matches": service_presence["route"] == identity if service_presence else None,
            "last_check_in_at": service_presence.get("last_client_contact_at") if service_presence else None,
            "enabled": state["enabled"] if state else False,
            "workspace_enabled": workspace_enabled, "binding_matches": binding_matches,
            "adapter_worker_running": adapter_worker_running,
            "participation_worker_running": participation_worker_running,
            "service_presence": service_presence, "service_error": service_error,
            "assured_sender_required": ["session", "slack-human"],
            "eligible": refs, "session_stopped": bool(session.get("stopped")),
            "wake_scan_cursor": scan_state["cursor"] if scan_state else None,
            "wake_scan_caught_up": scan_state["caught_up"] if scan_state else None,
            "wake_scan_compacted": scan_state["compacted"] if scan_state else None,
            "work_paused": bool(service_presence.get("work_paused")) if service_presence else None,
            "wake_readiness": readiness, "reason": reason,
            "host_mode": "standalone" if values.get("AGENTBUS_CODEX_HOST_MODE") == "standalone" else "notification_only",
            "turn_max_seconds": _turn_max_seconds(values),
            "last_terminal_status": last_status if last_status in {"completed", "interrupted", "failed"} else None,
            "host_turn_start": "unobserved",
            "observed_at": observed_at, "wake_epoch": state["wake_epoch"] if state else None,
            "accepted_wakes_last_hour": len(state["wake_history"]) if state else 0,
            "last_attempt": state.get("last_attempt") if state else None,
            "wake_intent": state.get("wake_intent") if state else None}


def run_once(values: dict[str, str], identity: str, *, live: bool = False,
             host_factory: HostFactory = CodexAppServer) -> dict:
    profile, session, path = _profile(values, identity)
    with _locked(path):
        state = _load(path)
        _validate_binding(state, profile, session)

        def finish(result: dict) -> dict:
            if live and record_result(state, profile, refs, result):
                _save(path, state)
            return result

        profile_path = identity_path(values, identity).resolve()
        scan_error = None
        scan_state = wake_inbox.read_state(values, profile, state["binding_generation"])
        token = profile_session_token(profile)
        if (live and state["enabled"] and workspace_status(values)["enabled"] and token and
                values.get("AGENTBUS_URL") and values.get("AGENTBUS_API_TOKEN")):
            try:
                scan_state = wake_inbox.scan(values, profile, state["binding_generation"], _candidate)
            except ClientError as exc:
                scan_error = str(exc)
                scan_state = wake_inbox.read_state(values, profile, state["binding_generation"])
        refs = eligible_events(profile_path, profile,
                               wake_inbox.candidates(scan_state, int(profile.get("ack_cursor", 0))))
        if live and profile.get('message_lifecycle_version') == 1:
            from src.agentbus_lifecycle_client import filter_wake_refs
            try:
                refs = filter_wake_refs(values, profile, refs, state, api_call=api)
                _save(path, state)
            except ClientError as exc:
                return {'status':'notification_only', 'reason':str(exc), 'eligible':refs}
        if not refs:
            if live and state.get('lifecycle_recovery_required'):
                return finish({'status':'notification_only', 'reason':'expired_work_lease_requires_owner_recovery', 'eligible':[]})
            if scan_error:
                return finish({"status": "service_unavailable", "reason": scan_error, "eligible": []})
            return finish({"status": "no_action", "eligible": []})
        fingerprint = _fingerprint(refs)
        if live and record_intent(state, "pending", refs):
            _save(path, state)
        if not live:
            return finish({"status": "dry_run", "eligible": refs, "enabled": state["enabled"]})
        if not workspace_status(values)["enabled"]:
            raise ClientError("Codex wake workspace is disabled")
        if not state["enabled"]:
            raise ClientError("Codex wake is disabled; enable it explicitly before --live")
        if host_factory is CodexAppServer and values.get("AGENTBUS_CODEX_HOST_MODE") != "standalone":
            return finish({"status": "notification_only", "reason": "owning_host_endpoint_unavailable",
                    "eligible": refs})
        try:
            presence = scoped_api(values, f"/v1/sessions/{session['session_id']}/self-presence", profile,
                           session_token=profile_session_token(profile), api_call=api)
        except ClientError:
            return finish({"status": "service_unavailable", "eligible": refs})
        if (presence.get("session_id") != session["session_id"] or
                presence.get("chat_id") != profile["chat_id"] or
                presence.get("route") != identity):
            return finish({"status": "service_identity_mismatch", "eligible": refs})
        if presence.get("state") == "stopped":
            return finish({"status": "session_stopped", "eligible": refs})
        if presence.get("state") not in {"active_compliant", "control_pending"}:
            return finish({"status": "service_session_not_ready", "eligible": refs})
        if not controls_projected(presence, refs):
            return finish({"status": "control_projection_pending", "eligible": refs})
        previous = state.get("last_attempt") or {}
        reconciled = False
        if previous.get("state") in {"starting", "uncertain"}:
            try:
                with host_factory() as host:
                    found = host.find_attempt(state["thread_id"], previous["attempt_id"])
            except CodexHostError:
                found = None
            if found is None:
                return finish({"status": "uncertain", "attempt_id": previous["attempt_id"],
                        "operator_action": "inspect host history; do not retry blindly"})
            terminal = found[1] if found[1] in {"completed", "interrupted", "failed"} else None
            previous.update(state=terminal or "accepted", turn_id=found[0], turn_status=found[1])
            state["last_attempt"] = previous
            state["last_fingerprint"] = previous["fingerprint"]
            if (not previous["candidate_ids"][0].startswith(("CONTROL:", "POLICY_CHANGED:")) and
                    previous["observed_at"] not in state["wake_history"]):
                state["wake_history"].append(previous["observed_at"])
            state["same_fingerprint_retries"] = 1 if previous.get("followup") else 0
            observe_host(state, state["thread_id"], found[0], found[1])
            _save(path, state)
            reconciled = True
        if previous.get("state") == "rejected" and previous.get("fingerprint") == fingerprint:
            return finish({"status": "turn_rejected", "operator_action": "inspect host rejection before retry"})
        followup = state.get("last_fingerprint") == fingerprint
        recovering_interrupted = False
        if followup:
            if reconciled:
                return finish({"status": "already_woken", "eligible": refs, "turn_id": previous.get("turn_id")})
            if state["same_fingerprint_retries"] >= 1:
                return finish({"status": "pending_after_followup", "eligible": refs,
                        "operator_action": "inspect unacknowledged work; no further automatic repeat"})
            last_attempt = state.get("last_attempt") or {}
            terminal_status = last_attempt.get("turn_status") if last_attempt.get("state") in {
                "completed", "interrupted", "failed"} else None
            if terminal_status is None:
                if last_attempt.get("state") != "accepted":
                    return finish({"status": "already_woken", "eligible": refs})
                try:
                    with host_factory() as host:
                        found = host.find_attempt(state["thread_id"], last_attempt["attempt_id"])
                except CodexHostError:
                    found = None
                if found is None or found[1] not in {"completed", "interrupted", "failed"}:
                    return finish({"status": "already_woken", "eligible": refs,
                            "turn_id": last_attempt.get("turn_id"),
                            "host_turn_status": found[1] if found else "unknown"})
                terminal_status = found[1]
                last_attempt.update(state=terminal_status, turn_status=terminal_status)
                if (not last_attempt["candidate_ids"][0].startswith(("CONTROL:", "POLICY_CHANGED:")) and
                        last_attempt["observed_at"] not in state["wake_history"]):
                    state["wake_history"].append(last_attempt["observed_at"])
                _save(path, state)
            recovering_interrupted = terminal_status in {"interrupted", "failed"}
            # Turn completion is only host evidence. The still-pending bus
            # events merit one bounded follow-up, never an inferred ack.
        # Required controls and policy revisions retain their safety path even
        # when ordinary chat traffic has exhausted its autonomous budget.
        rate = None if refs[0].startswith(("CONTROL:", "POLICY_CHANGED:")) else _rate_result(state)
        if rate is not None:
            return finish(rate)
        try:
            with host_factory() as host:
                thread = host.read_thread(state["thread_id"])
                if not isinstance(thread, dict) or thread.get("id") != state["thread_id"] or thread.get("ephemeral") is not False:
                    return finish({"status": "host_identity_mismatch", "eligible": refs})
                status = thread.get("status")
                host_state = status.get("type") if isinstance(status, dict) else None
                if host_state == "active":
                    return finish({"status": "thread_active", "eligible": refs})
                if host_state != "idle":
                    return finish({"status": "host_not_idle", "host_state": host_state})
                try:
                    host.resume(state["thread_id"])
                except CodexHostRejected:
                    return finish({"status": "thread_owned_by_host", "eligible": refs})
                from src.agentbus_lifecycle_client import pickup_wake_refs
                try:
                    pickup_wake_refs(values, profile, refs, api_call=api)
                except ClientError as exc:
                    return finish({'status':'lifecycle_wait', 'reason':str(exc), 'eligible':refs})
                record_intent(state, "eligible", refs, reason="exact_ready_session_and_idle_owner")
                attempt_id = str(uuid.uuid4())
                state["wake_epoch"] += 1
                attempt = {"attempt_id": attempt_id, "fingerprint": fingerprint,
                           "candidate_ids": refs, "state": "starting", "observed_at": _now(),
                           "turn_id": None, "wake_epoch": state["wake_epoch"],
                           "followup": followup, **scope(profile)}
                state["last_attempt"] = attempt
                record_intent(state, "submitting", refs, reason=attempt_id)
                _save(path, state)
                attach_observer(host, state, path, _save)
                # The marker lets a later host-history read prove a turn started
                # if the response is lost after the request crossed the wire.
                try:
                    turn_id = host.start_turn(
                        state["thread_id"], _prompt(identity, attempt_id, refs,
                                                    work_paused=bool(presence.get("work_paused")),
                                                    ack_cursor=int(profile.get("ack_cursor", 0)),
                                                    recovering_interrupted=recovering_interrupted))
                    attempt.update(state="accepted", turn_id=turn_id, accepted_at=_now())
                    state["last_attempt"] = attempt
                    record_intent(state, "admitted", refs, reason="exact_host_turn_id", turn_id=turn_id)
                    observe_host(state, state["thread_id"], turn_id, getattr(host, "last_turn_status", "unknown"))
                    if not refs[0].startswith(("CONTROL:", "POLICY_CHANGED:")):
                        state["wake_history"].append(attempt["observed_at"])
                    _save(path, state)
                    turn_status = host.await_turn_terminal(
                        state["thread_id"], turn_id, max_seconds=_turn_max_seconds(values))
                    if turn_status not in {"completed", "interrupted", "failed"}:
                        raise TurnCompletionUncertain("Codex host returned an invalid terminal status")
                except CodexHostRejected as exc:
                    attempt["state"] = "rejected"
                    state["last_attempt"] = attempt
                    _save(path, state)
                    return finish({"status": "turn_rejected", "reason": str(exc)})
        except (TurnStartUncertain, TurnCompletionUncertain) as exc:
            attempt["state"] = "uncertain"
            state["last_attempt"] = attempt
            _save(path, state)
            return finish({"status": "turn_completion_unobserved" if isinstance(exc, TurnCompletionUncertain) else "uncertain",
                    "attempt_id": attempt_id, "turn_id": attempt.get("turn_id"),
                    "operator_action": "inspect host history; do not retry blindly"})
        except CodexHostError as exc:
            current_attempt = state.get("last_attempt")
            if current_attempt and current_attempt.get("state") == "starting":
                current_attempt["state"] = "resume_failed"
                _save(path, state)
            return finish({"status": "host_unavailable", "reason": str(exc)})
        attempt.update(state=turn_status, turn_status=turn_status)
        state["last_attempt"] = attempt
        state["last_fingerprint"] = fingerprint
        state["same_fingerprint_retries"] = 1 if followup else 0
        _save(path, state)
        return finish({"status": f"turn_{turn_status}", "host_turn_status": turn_status, "turn_id": turn_id,
                "attempt_id": attempt_id, "wake_epoch": state["wake_epoch"], "eligible": refs})


def cli_codex_wake(ns: argparse.Namespace, _project: Path, values: dict[str, str]) -> int:
    if ns.wake_command == "workspace-enable":
        result = set_workspace_enabled(values, True)
        try:
            result.update(start_worker(_project, values))
        except ClientError:
            set_workspace_enabled(values, False)
            raise
    elif ns.wake_command == "workspace-disable":
        result = set_workspace_enabled(values, False)
        result.update(stop_worker(values))
    elif ns.wake_command == "workspace-status":
        result = workspace_status(values)
        result["worker_pid"] = _worker_pid(_worker_path(values))
    elif ns.wake_command == "watch-all":
        watch_all(values, ns.interval)
        return 0
    else:
        identity = resolve_identity(ns.identity)
        return _cli_bound_wake(ns, values, identity)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _cli_bound_wake(ns: argparse.Namespace, values: dict[str, str], identity: str) -> int:
    if ns.wake_command == "enroll-current":
        result = enroll_current(values, identity)
    elif ns.wake_command == "bind":
        result = bind(values, identity, ns.thread)
    elif ns.wake_command == "enable":
        result = set_enabled(values, identity, True)
    elif ns.wake_command == "disable":
        result = set_enabled(values, identity, False)
    elif ns.wake_command == "status":
        result = status(values, identity)
    elif ns.wake_command == "once":
        result = run_once(values, identity, live=ns.live)
    elif ns.wake_command == "watch":
        if not math.isfinite(ns.interval) or not 1 <= ns.interval <= 60:
            raise ClientError("Codex wake watch interval must be between 1 and 60 seconds")
        last: str | None = None
        while True:
            result = run_once(values, identity, live=ns.live)
            encoded = json.dumps(result, sort_keys=True)
            if encoded != last and result["status"] != "no_action":
                print(encoded, flush=True)
            last = encoded
            time.sleep(ns.interval)
    else:
        raise ClientError("Unknown Codex wake operation")
    print(json.dumps(result, ensure_ascii=False))
    return 0


def watch_all(values: dict[str, str], interval: float = 2.0) -> None:
    if not math.isfinite(interval) or not 1 <= interval <= 60:
        raise ClientError("Codex wake watch interval must be between 1 and 60 seconds")
    last_results: dict[str, str] = {}
    active: dict[str, threading.Thread] = {}
    finished: queue.Queue[tuple[str, dict | str]] = queue.Queue()
    stopping = threading.Event()
    previous_handler = None
    if threading.current_thread() is threading.main_thread():
        previous_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda _signum, _frame: stopping.set())

    def wake_one(identity: str) -> None:
        try:
            finished.put((identity, run_once(values, identity, live=True)))
        except (ClientError, OSError, ValueError) as exc:
            finished.put((identity, str(exc)))

    try:
        _watch_all_loop(values, interval, active, finished, last_results, wake_one, stopping)
    finally:
        for worker in active.values():
            worker.join()
        if previous_handler is not None:
            signal.signal(signal.SIGTERM, previous_handler)


def _watch_all_loop(values: dict[str, str], interval: float, active: dict[str, threading.Thread],
                    finished: queue.Queue[tuple[str, dict | str]], last_results: dict[str, str],
                    wake_one: Callable[[str], None], stopping: threading.Event) -> None:
    while not stopping.is_set() and workspace_status(values)["enabled"]:
        while not finished.empty():
            finished_identity, outcome = finished.get_nowait()
            encoded = json.dumps(outcome, sort_keys=True) if isinstance(outcome, dict) else outcome
            if encoded != last_results.get(finished_identity):
                if isinstance(outcome, dict):
                    if outcome["status"] not in {"no_action", "already_woken"}:
                        print(json.dumps({"identity": finished_identity, **outcome}), flush=True)
                else:
                    print(json.dumps({"identity": finished_identity, "error": outcome}), flush=True)
            last_results[finished_identity] = encoded
        root = consumer_state_dir(values)
        for profile_path in root.glob("*.json"):
            if profile_path.is_symlink():
                continue
            try:
                profile = json.loads(profile_path.read_text())
                identity = profile.get("identity") if isinstance(profile, dict) else None
                if not isinstance(identity, str) or not isinstance(profile.get("participation"), dict) or profile["participation"].get("stopped"):
                    continue
                chat_id = profile.get("chat_id")
                if not isinstance(chat_id, str) or not _state_path(values, chat_id).is_file():
                    continue
                ensure_worker(profile_path, values)
                if stopping.is_set():
                    break
                if identity not in active or not active[identity].is_alive():
                    worker = threading.Thread(target=wake_one, args=(identity,))
                    active[identity] = worker
                    worker.start()
            except (ClientError, OSError, ValueError) as exc:
                encoded = str(exc)
                if encoded != last_results.get(profile_path.name):
                    print(json.dumps({"profile": profile_path.name, "error": encoded}), flush=True)
                last_results[profile_path.name] = encoded
        stopping.wait(interval)
