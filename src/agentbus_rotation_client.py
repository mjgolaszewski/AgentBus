"""Private, retryable client handoff for participation credential rotation."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from src.agentbus_client import checked_profile, save_profile
from src.agentbus_transport_client import ClientError, api


def _private_token(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ClientError("Rotation file must be a regular private file (mode 0600).")
    token = path.read_text().strip()
    if len(token) < 32:
        raise ClientError("Rotation file has no valid token.")
    return token


def issue_rotation(identity: str, output: str, values: dict[str, str]) -> int:
    """Only the operator receives the grant and writes it to a private file."""
    profile, _ = checked_profile(values, identity)
    session = profile.get("participation")
    if not session or session.get("stopped"):
        raise ClientError("This chat has no live participation session.")
    operator_token = os.environ.get("AGENTBUS_OPERATOR_TOKEN", "")
    if not operator_token or operator_token == values.get("AGENTBUS_API_TOKEN"):
        raise ClientError("Export a separate AGENTBUS_OPERATOR_TOKEN for credential rotation.")
    destination = Path(output)
    if destination.is_symlink() or destination.exists():
        raise ClientError("Rotation output already exists; choose a new private path.")
    result = api(values, f"/v1/sessions/{session['session_id']}/rotation-grants",
                 {"chat_id": profile["chat_id"]}, bearer_token=operator_token)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(result["rotation_token"] + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print(f"Rotation grant written to {destination}; expires in {result['expires_in_seconds']} seconds.")
    return 0


def rotate_secret(identity: str, rotation_file: str | None, replace_pending: bool,
                  values: dict[str, str]) -> int:
    """Stage the new secret before sending so lost responses are retryable."""
    profile, _ = checked_profile(values, identity)
    session = profile.get("participation")
    if not session or session.get("stopped"):
        raise ClientError("This chat has no live participation session.")
    pending = profile.get("participation_rotation_pending")
    if replace_pending and not rotation_file:
        raise ClientError("Replacing a pending rotation requires --rotation-file.")
    if pending is not None and pending.get("session_id") != session["session_id"]:
        raise ClientError("Pending rotation belongs to a different session.")
    if pending is None or replace_pending:
        if rotation_file is None:
            raise ClientError("Rotation requires an operator-issued --rotation-file.")
        pending = {"session_id": session["session_id"],
                   "rotation_token": _private_token(Path(rotation_file)),
                   "new_session_secret": secrets.token_urlsafe(32)}
        profile["participation_rotation_pending"] = pending
        save_profile(values, profile)
    result = api(values, f"/v1/sessions/{session['session_id']}/rotate-secret",
                 {"rotation_token": pending["rotation_token"],
                  "new_session_secret": pending["new_session_secret"]})
    if result["session_id"] != session["session_id"]:
        raise ClientError("Rotation receipt belongs to a different session.")
    session["session_secret"] = pending["new_session_secret"]
    profile.pop("participation_rotation_pending", None)
    save_profile(values, profile)
    print(f"ACK {result['receipt']} SESSION_CREDENTIAL_ROTATED")
    return 0
