"""Crash-retryable local handoff after an authenticated routing rename."""

from __future__ import annotations

import fcntl
import json
import os
import re

from src.agentbus_poll_client import _append, _worker_lock, change_spool, ensure_worker, spool_path
from src.agentbus_transport_client import ClientError, api

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def rename_chat(ns, values: dict[str, str]) -> int:
    from src.agentbus_client import (
        atomic_json,
        checked_profile,
        identity_path,
        resolve_identity,
        save_profile,
    )

    old_route = resolve_identity(ns.identity)
    profile, _ = checked_profile(values, old_route)
    session = profile.get("participation")
    if not session or session.get("stopped"):
        raise ClientError("Routing rename requires an active participation session.")
    if not NAME.fullmatch(ns.name):
        raise ClientError("Chat names must be lowercase letters, digits, or hyphens (maximum 32).")
    new_route = f"{profile['repo']}:{ns.name}"
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
                 {"route": new_route}, session_token=session["session_secret"])
    if result.get("chat_id") != profile["chat_id"] or result.get("new_route") != new_route:
        raise ClientError("Service rename receipt does not match this chat.")
    directory_lock = old_path.parent / ".profiles.lock"
    if directory_lock.is_symlink():
        raise ClientError("Refusing symlinked profile lock.")
    with directory_lock.open("a") as lock:
        os.chmod(directory_lock, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        updated = dict(profile)
        updated.update(identity=new_route, name=ns.name)
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
    recover_rename_state(values, json.loads(new_path.read_text()))
    print(f"ROUTE {old_route} -> {new_route}")
    return 0


def recover_rename_state(values: dict[str, str], profile: dict) -> None:
    """Finish a local rename after a process dies between receipt and spool move."""
    source = profile.get("rename_source")
    if source is None:
        return
    from src.agentbus_client import identity_path, save_profile

    old_path = identity_path(values, source)
    new_path = identity_path(values, profile["identity"])
    directory_lock = old_path.parent / ".profiles.lock"
    if directory_lock.is_symlink() or old_path.is_symlink():
        raise ClientError("Refusing unsafe profile path during rename recovery.")
    with directory_lock.open("a") as lock:
        os.chmod(directory_lock, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if old_path.is_symlink() or new_path.is_symlink():
            raise ClientError("Refusing unsafe profile path during rename recovery.")
        if old_path.exists():
            try:
                previous = json.loads(old_path.read_text())
            except (OSError, ValueError):
                raise ClientError("Old profile needs manual recovery before route handoff.") from None
            if previous.get("chat_id") != profile.get("chat_id"):
                raise ClientError("Old route now belongs to another local chat.")
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
