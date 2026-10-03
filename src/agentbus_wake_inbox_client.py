"""Private, non-acknowledging service inbox cursor for Codex wake discovery."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from src.agentbus_client import atomic_json, consumer_state_dir
from src.agentbus_transport_client import ClientError, api, profile_session_token

MAX_CANDIDATES = 64
PAGES_PER_SCAN = 8
Classifier = Callable[[dict, str], tuple[int, str] | None]


def state_path(values: dict[str, str], chat_id: str) -> Path:
    try:
        chat_id = str(uuid.UUID(chat_id))
    except ValueError:
        raise ClientError("Codex wake scan needs a valid stable chat UUID") from None
    directory = consumer_state_dir(values) / "codex-wake"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ClientError("Refusing unsafe Codex wake state directory")
    return directory / f"{chat_id}.inbox.json"


def _fresh(profile: dict, generation: int) -> dict:
    session = profile["participation"]
    return {
        "schema_version": 1,
        "chat_id": profile["chat_id"],
        "session_id": session["session_id"],
        "binding_generation": generation,
        "cursor": int(profile.get("ack_cursor", 0)),
        "pending": [],
        "compacted": False,
        "caught_up": False,
    }


def read_state(values: dict[str, str], profile: dict, generation: int) -> dict:
    """Read a projection without creating or advancing it."""
    path = state_path(values, profile["chat_id"])
    if path.is_symlink():
        raise ClientError("Refusing symlinked Codex wake scan state")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _fresh(profile, generation)
    except (OSError, ValueError):
        raise ClientError("Codex wake scan state is unreadable") from None
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ClientError("Codex wake scan state has an unsupported schema")
    if (state.get("chat_id") != profile["chat_id"] or
            state.get("session_id") != profile["participation"]["session_id"] or
            state.get("binding_generation") != generation):
        return _fresh(profile, generation)
    pending = state.get("pending")
    if (not isinstance(state.get("cursor"), int) or state["cursor"] < 0 or
            not isinstance(pending, list) or len(pending) > MAX_CANDIDATES or
            not isinstance(state.get("compacted"), bool) or
            not isinstance(state.get("caught_up"), bool)):
        raise ClientError("Codex wake scan state is invalid")
    for item in pending:
        if (not isinstance(item, dict) or not isinstance(item.get("cursor"), int) or
                item["cursor"] < 0 or item["cursor"] > state["cursor"] or
                item.get("priority") not in {2, 3, 4}):
            raise ClientError("Codex wake scan candidates are invalid")
    return state


def candidates(state: dict, ack_cursor: int) -> list[tuple[int, str]]:
    return [(item["priority"], f"MESSAGE:{item['cursor']}")
            for item in state["pending"] if item["cursor"] > ack_cursor]


def scan(values: dict[str, str], profile: dict, generation: int,
         classify: Classifier, *, page_budget: int = PAGES_PER_SCAN) -> dict:
    """Persist candidates and cursor together, without consuming message truth."""
    token = profile_session_token(profile)
    if not token:
        raise ClientError("Session proof is required for Codex wake inbox scanning")
    if not 1 <= page_budget <= 100:
        raise ClientError("Codex wake page budget is invalid")
    path = state_path(values, profile["chat_id"])
    state = read_state(values, profile, generation)
    ack_cursor = int(profile.get("ack_cursor", 0))
    state["pending"] = [item for item in state["pending"] if item["cursor"] > ack_cursor]
    state["cursor"] = max(state["cursor"], ack_cursor)
    for _ in range(page_budget):
        after = state["cursor"]
        page = api(values, "/v1/inbox?" + urlencode({
            "identity": profile["identity"], "after": after, "limit": 100,
        }), session_token=token)
        messages = page.get("messages")
        next_cursor = page.get("next_cursor")
        has_more = page.get("has_more")
        if (not isinstance(messages, list) or not isinstance(next_cursor, int) or
                not isinstance(has_more, bool) or next_cursor < after or
                (has_more and next_cursor == after)):
            raise ClientError("Codex wake inbox page is invalid")
        seen = {item["cursor"] for item in state["pending"]}
        previous_cursor = after
        for message in messages:
            if not isinstance(message, dict) or not isinstance(message.get("cursor"), int):
                raise ClientError("Codex wake inbox message is invalid")
            cursor = message["cursor"]
            if cursor <= previous_cursor or cursor > next_cursor:
                raise ClientError("Codex wake inbox cursor is invalid")
            previous_cursor = cursor
            candidate = classify({"kind": "MESSAGE", "id": str(cursor), "message": message},
                                 profile["identity"])
            if candidate and cursor > ack_cursor and cursor not in seen:
                state["pending"].append({"cursor": cursor, "priority": candidate[0]})
                seen.add(cursor)
        state["pending"].sort(key=lambda item: item["cursor"])
        if len(state["pending"]) > MAX_CANDIDATES:
            state["pending"] = state["pending"][-MAX_CANDIDATES:]
            state["compacted"] = True
        state["cursor"] = next_cursor
        state["caught_up"] = not has_more
        # A page and its candidate identities are one durable transition.
        atomic_json(path, state)
        if not has_more:
            break
    return state
