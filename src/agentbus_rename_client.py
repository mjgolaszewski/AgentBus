"""Crash-retryable local handoff after an authenticated routing rename."""

from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import ExitStack, contextmanager

from src.agentbus_poll_client import _append, _worker_lock, change_spool, ensure_worker, spool_path
from src.agentbus_profile_client import _authority
from src.agentbus_scope_client import scope, verify_projection
from src.agentbus_transport_client import ClientError, api, local_prefix

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


@contextmanager
def _profile_locks(*paths):
    """Directory handoff lock precedes stable, sorted per-profile locks."""
    with ExitStack() as stack:
        for path in sorted(set(paths)):
            lock_path = path.parent / f".{path.name}.lock"
            if path.is_symlink() or lock_path.is_symlink():
                raise ClientError("Refusing unsafe profile path during rename recovery.")
            lock = stack.enter_context(lock_path.open("a"))
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _handoff_state(profile):
    ignored = {"identity", "name", "rename_pending", "rename_source", "updated_at",
               "ack_cursor", "observed_cursor", "scope_verified"}
    return {key: value for key, value in profile.items() if key not in ignored}


def rename_chat(ns, values: dict[str, str]) -> int:
    from src.agentbus_client import (
        atomic_json,
        checked_profile,
        identity_path,
        load_profile,
        resolve_identity,
        save_profile,
    )

    old_route = resolve_identity(ns.identity)
    profile, _ = checked_profile(values, old_route)
    session = profile.get("participation")
    if not session or session.get("stopped"):
        raise ClientError("Routing rename requires an active participation session.")
    name = ns.name or profile["name"]
    if not NAME.fullmatch(name):
        raise ClientError("Chat names must be lowercase letters, digits, or hyphens (maximum 32).")
    prefix = local_prefix(values) or (old_route.split(":")[0] if old_route.count(":") == 2 else None)
    new_route = f"{prefix + ':' if prefix else ''}{profile['repo']}:{name}"
    if len(new_route) > 80:
        raise ClientError("Combined AgentBus identity exceeds 80 characters.")
    if new_route == old_route:
        print(f"ROUTE {new_route}")
        return 0
    if profile.get("rename_pending") not in {None, new_route}:
        raise ClientError("A different rename is pending; recover it before choosing another name.")
    old_path = identity_path(values, old_route)
    new_path = identity_path(values, new_route)
    if new_path.is_symlink():
        raise ClientError("Refusing symlinked target profile.")
    if new_path.exists():
        try:
            existing = json.loads(new_path.read_text())
        except (OSError, ValueError):
            raise ClientError("Target local profile cannot be read safely.") from None
        if existing.get("chat_id") != profile["chat_id"]:
            raise ClientError("Target local profile belongs to another chat.")
    if profile.get("rename_pending") is None:
        profile["rename_pending"] = new_route
        save_profile(values, profile)
    result = api(values, f"/v1/sessions/{session['session_id']}/rename",
                 {"route": new_route}, session_token=session["session_secret"], inbox_id=profile["inbox_id"])
    if result.get("chat_id") != profile["chat_id"] or result.get("new_route") != new_route:
        raise ClientError("Service rename receipt does not match this chat.")
    directory_lock = old_path.parent / ".profiles.lock"
    if directory_lock.is_symlink():
        raise ClientError("Refusing symlinked profile lock.")
    with directory_lock.open("a") as lock:
        os.chmod(directory_lock, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with _profile_locks(old_path, new_path):
            fresh = load_profile(values, old_route, save_migration=False)
            if _authority(fresh) != _authority(profile):
                raise ClientError("Rename session authority changed; both profiles retained for recovery")
            updated = dict(fresh)
            updated.update(identity=new_route, name=name)
            updated.pop("rename_pending", None)
            updated["rename_source"] = old_route
            if new_path.is_symlink():
                raise ClientError("Refusing symlinked target profile.")
            if new_path.exists():
                try:
                    existing = json.loads(new_path.read_text())
                except (OSError, ValueError):
                    raise ClientError("Target local profile cannot be read safely.") from None
                if existing.get("chat_id") != profile["chat_id"]:
                    raise ClientError("Target local profile belongs to another chat.")
                if old_path.exists() and existing.get("rename_source") != old_route:
                    existing["rename_source"] = old_route
                    atomic_json(new_path, existing)
            else:
                atomic_json(new_path, updated)
    recover_rename_state(values, load_profile(values, new_route, save_migration=False))
    print(f"ROUTE {old_route} -> {new_route}")
    return 0


def recover_rename_state(values: dict[str, str], profile: dict) -> None:
    """Finish a local rename after a process dies between receipt and spool move."""
    source = profile.get("rename_source")
    if source is None:
        return
    if not isinstance(source, str) or not source or source == profile.get("identity"):
        raise ClientError("Invalid rename source; evidence retained")
    from src.agentbus_client import identity_path, save_profile

    old_path = identity_path(values, source)
    new_path = identity_path(values, profile["identity"])
    directory_lock = old_path.parent / ".profiles.lock"
    if directory_lock.is_symlink() or old_path.is_symlink():
        raise ClientError("Refusing unsafe profile path during rename recovery.")
    with directory_lock.open("a") as lock:
        os.chmod(directory_lock, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with _profile_locks(old_path, new_path):
            current = json.loads(new_path.read_text())
            if current.get("rename_source") != source or _handoff_state(current) != _handoff_state(profile):
                raise ClientError("Rename target changed; both profiles retained for recovery")
            verified = profile.get("scope_verified") is True
            profile.clear()
            profile.update(current)
            if verified:
                profile["scope_verified"] = True
            if old_path.is_symlink() or new_path.is_symlink():
                raise ClientError("Refusing unsafe profile path during rename recovery.")
            if old_path.exists():
                try:
                    previous = json.loads(old_path.read_text())
                except (OSError, ValueError):
                    raise ClientError("Old profile needs manual recovery before route handoff.") from None
                if scope(previous) != scope(profile):
                    raise ClientError("Rename profile scope mismatch; evidence retained")
                if previous.get("chat_id") != profile.get("chat_id"):
                    raise ClientError("Old route now belongs to another local chat.")
                if _handoff_state(previous) != _handoff_state(profile):
                    raise ClientError("Rename recovery state changed; both profiles retained for recovery")
                for field in ("ack_cursor", "observed_cursor"):
                    before, after = previous.get(field, 0), profile.get(field, 0)
                    if type(before) is not int or type(after) is not int or min(before, after) < 0:
                        raise ClientError("Invalid rename cursor; evidence retained")
                    profile[field] = max(before, after)
            prior_spool = spool_path(old_path)
            if prior_spool.exists():
                verify_projection(json.loads(prior_spool.read_text()), profile)
            from src.agentbus_client import atomic_json

            atomic_json(new_path, profile)
            old_path.unlink(missing_ok=True)
    # The old worker owns the old path and exits when it disappears. Wait for
    # its lifetime lock before moving its undelivered semantic events.
    worker_lock = _worker_lock(old_path)
    if worker_lock.is_symlink():
        raise ClientError("Refusing symlinked worker lock.")
    with worker_lock.open("a") as lock:
        os.chmod(worker_lock, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        old_spool = spool_path(old_path)
        if old_spool.is_symlink():
            raise ClientError("Refusing symlinked poll state.")
        if old_spool.exists():
            try:
                old_state = json.loads(old_spool.read_text())
                verify_projection(old_state, profile)
            except (OSError, ValueError):
                raise ClientError("Old poll state needs recovery before the new worker starts.") from None

            def merge(state: dict) -> None:
                state["cursor"] = max(int(state["cursor"]), int(old_state["cursor"]))
                for event in old_state["events"]:
                    _append(state, event)

            change_spool(new_path, profile, merge)
            old_spool.unlink()
    profile.pop("rename_source", None)
    save_profile(values, profile)
    session = profile.get("participation") or {}
    if session.get("acknowledged_policy_revision") and not session.get("stopped"):
        ensure_worker(new_path, values)
