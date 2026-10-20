"""Bounded wake-intent observations inside the existing private binding file."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.agentbus_scope_client import FIELDS, scope
from src.agentbus_transport_client import ClientError

STATES = {"pending", "eligible", "submitting", "uncertain", "queued", "admitted",
          "running", "completed", "acknowledged", "blocked"}
IN_FLIGHT = {"submitting", "uncertain", "queued", "admitted", "running"}


def recover_legacy_scope(binding: dict, profile: dict | None = None) -> dict:
    """Project recognized legacy scope; only an authenticated command may persist it."""
    absent = set(FIELDS) - binding.keys()
    legacy = {"service_url", "inbox_id", "channel"}
    if absent and absent != legacy:
        raise ClientError("Legacy wake binding scope cannot be recovered; evidence retained")
    expected = scope(profile) if profile is not None else {
        key: binding[key] for key in FIELDS if key in binding}
    if any(key in binding and binding[key] != value for key, value in expected.items()):
        raise ClientError("Wake binding scope differs from the authenticated profile; evidence retained")
    recovered = {**binding, **expected} if absent and profile is not None else binding
    for name in ("wake_intent", "last_attempt"):
        record = binding.get(name)
        if record is None:
            continue
        if not isinstance(record, dict):
            raise ClientError("Legacy wake record is invalid; evidence retained")
        missing = set(FIELDS) - record.keys()
        allowed = ({"chat_id"}, legacy | {"chat_id"}) if name == "wake_intent" else (set(FIELDS),)
        if missing and (binding.get("schema_version") != 1 or missing not in allowed
                        or (missing != {"chat_id"} and absent != legacy)):
            raise ClientError("Legacy wake record scope cannot be recovered; evidence retained")
        if any(key in record and record[key] != value for key, value in expected.items()):
            raise ClientError("Codex wake intent has invalid binding or state; evidence retained")
        if any(key in record and record[key] != binding[key]
               for key in ("thread_id", "binding_generation")):
            raise ClientError("Codex wake record has invalid binding; evidence retained")
        if missing:
            if name == "wake_intent":
                if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("id", ""))):
                    raise ClientError("Legacy wake intent identity is invalid; evidence retained")
            elif (not isinstance(record.get("attempt_id"), str) or not record["attempt_id"]
                  or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("fingerprint", "")))
                  or not isinstance(record.get("candidate_ids"), list) or not record["candidate_ids"]
                  or not all(isinstance(ref, str) and ref for ref in record["candidate_ids"])
                  or record.get("state") not in {"starting", "uncertain", "accepted", "rejected",
                                                 "completed", "interrupted", "failed"}):
                raise ClientError("Legacy wake attempt is invalid; evidence retained")
            if recovered is binding:
                recovered = dict(binding)
            recovered[name] = {**record, **expected}
    validate_intent(recovered)
    return recovered


def validate_intent(binding: dict) -> None:
    intent = binding.get("wake_intent")
    if intent is None:
        return  # Existing schema-1 bindings migrate on their first observed intent.
    if (not isinstance(intent, dict) or intent.get("state") not in STATES
            or intent.get("session_id") != binding["session_id"]
            or intent.get("thread_id") != binding["thread_id"]
            or intent.get("binding_generation") != binding["binding_generation"]
            or any(intent.get(key) != binding.get(key) for key in FIELDS)
            or not isinstance(intent.get("candidate_ids"), list)
            or not intent["candidate_ids"]
            or not all(isinstance(ref, str) for ref in intent["candidate_ids"])
            or not isinstance(intent.get("transitions"), list)
            or not 1 <= len(intent["transitions"]) <= 32):
        raise ClientError("Codex wake intent has invalid binding or state")
    for transition in intent["transitions"]:
        if not isinstance(transition, dict) or transition.get("state") not in STATES:
            raise ClientError("Codex wake intent has invalid transition history")


def record_intent(binding: dict, phase: str, refs: list[str], *, reason: str = "",
                  turn_id: str | None = None) -> bool:
    """Record an actual observation, never synthesize intermediate host facts."""
    validate_intent(binding)
    if phase not in STATES or not refs:
        raise ClientError("Codex wake intent observation is invalid")
    previous = binding.get("wake_intent")
    if previous and previous["state"] in IN_FLIGHT and phase == "pending":
        return False  # New pending work cannot supersede an unresolved host attempt.
    if previous and previous["candidate_ids"] == refs and phase == "pending":
        return False
    if previous and previous["state"] in IN_FLIGHT and phase in {"blocked", "uncertain"}:
        refs = previous["candidate_ids"]
    if previous is None or previous["candidate_ids"] != refs:
        identity = [*(binding[key] for key in FIELDS), binding["thread_id"], binding["binding_generation"], refs]
        binding["wake_intent"] = {
            "id": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
            "session_id": binding["session_id"], "thread_id": binding["thread_id"],
            "binding_generation": binding["binding_generation"], "candidate_ids": list(refs),
            **{key: binding[key] for key in FIELDS},
            "state": phase, "transitions": [],
        }
    intent = binding["wake_intent"]
    event = {"state": phase, "reason": reason, "turn_id": turn_id}
    if intent["transitions"] and all(intent["transitions"][-1].get(key) == value for key, value in event.items()):
        return False
    intent["state"] = phase
    intent["transitions"] = (intent["transitions"] + [
        {**event, "observed_at": datetime.now(timezone.utc).isoformat()},
    ])[-32:]
    return True


def observe_host(binding: dict, thread_id: str, turn_id: str, status: str) -> bool:
    attempt = binding.get("last_attempt") or {}
    if thread_id != binding["thread_id"] or turn_id != attempt.get("turn_id"):
        return False
    phase = {"queued": "queued", "inProgress": "running", "completed": "completed",
             "interrupted": "blocked", "failed": "blocked"}.get(status)
    current = (binding.get("wake_intent") or {}).get("state")
    if ((current in {"completed", "acknowledged"} and phase in {"queued", "running"})
            or (current == "running" and phase == "queued")):
        return False
    return bool(phase and record_intent(binding, phase, attempt["candidate_ids"],
                                        reason=f"host:{status}", turn_id=turn_id))


def attach_observer(host, binding: dict, path: Path, save: Callable) -> None:
    """Only exact host facts can update execution state while the adapter waits."""
    setter = getattr(host, "set_turn_observer", None)
    if setter is not None:
        def observed(thread_id: str, turn_id: str, status: str) -> None:
            if observe_host(binding, thread_id, turn_id, status):
                save(path, binding)
        setter(observed)


def record_result(binding: dict, profile: dict, refs: list[str], result: dict) -> bool:
    status = result["status"]
    if status == "control_projection_pending" and (binding.get("wake_intent") or {}).get("state") in IN_FLIGHT:
        return False
    if status == "no_action":
        intent = binding.get("wake_intent")
        pending = intent["candidate_ids"] if intent else []
        ack = int(profile.get("ack_cursor", 0))
        if pending and all(ref.startswith("MESSAGE:") and ref[8:].isdecimal()
                           and int(ref[8:]) <= ack for ref in pending):
            return record_intent(binding, "acknowledged", pending, reason=f"semantic_ack:{ack}")
        return False
    if not refs or status in {"dry_run", "already_woken"}:
        return False
    phase = {"uncertain": "uncertain", "turn_completion_unobserved": "uncertain",
             "turn_completed": "completed"}.get(status, "blocked")
    return record_intent(binding, phase, refs, reason=status,
                         turn_id=(binding.get("last_attempt") or {}).get("turn_id"))
