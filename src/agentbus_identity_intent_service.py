"""One immutable operator intent; decoding never grants operator authority."""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from uuid import UUID

ACTIONS = frozenset({'rename', 'retire', 'retire_with_history'})
FIELDS = frozenset({'version', 'request_id', 'actor', 'channel', 'inbox_id', 'chat_id', 'revision',
                    'action', 'created_at', 'expires_at', 'name', 'session_ids', 'positions'})
DENIAL = 'Identity operation unavailable; reopen the identity manager.'


@dataclass(frozen=True)
class IdentityManagementIntent:
    version: int
    request_id: str
    actor: str
    channel: str
    inbox_id: str
    chat_id: str
    revision: int
    action: str
    created_at: float
    expires_at: float
    name: str | None
    session_ids: tuple[str, ...]
    positions: tuple[int, ...]


def _uuid(value):
    if not isinstance(value, str) or len(value) != 36 or str(UUID(value)) != value:
        raise ValueError(DENIAL)
    return value


def decode_identity_intent(value, *, actor, channel, inbox_id):
    """Bind immutable preview data to the caller's already verified context."""
    try:
        if (not isinstance(value, dict) or len(value) != len(FIELDS) or set(value) != FIELDS
                or type(value['version']) is not int or value['version'] != 1
                or not isinstance(actor, str) or not 1 <= len(actor) <= 128
                or value['actor'] != actor or value['channel'] != channel or value['inbox_id'] != inbox_id
                or not isinstance(channel, str) or not re.fullmatch(r'[CG][A-Z0-9]{1,79}', channel)
                or type(value['revision']) is not int or not 0 <= value['revision'] < 2**63
                or not isinstance(value['action'], str) or value['action'] not in ACTIONS):
            raise ValueError(DENIAL)
        created, expires = value['created_at'], value['expires_at']
        if (any(isinstance(t, bool) or not isinstance(t, (int, float)) or not math.isfinite(t)
                for t in (created, expires)) or not 0 <= created < expires or expires-created > 600):
            raise ValueError(DENIAL)
        name = value['name']
        if value['action'] == 'rename':
            if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,31}', name):
                raise ValueError(DENIAL)
        elif name is not None:
            raise ValueError(DENIAL)
        sessions, positions = value['session_ids'], value['positions']
        if (not isinstance(sessions, list) or len(sessions) > 256
                or not isinstance(positions, list) or len(positions) > 100):
            raise ValueError(DENIAL)
        sessions = tuple(_uuid(item) for item in sessions)
        if len(set(sessions)) != len(sessions):
            raise ValueError(DENIAL)
        if (any(type(item) is not int or not 0 < item < 2**63 for item in positions)
                or len(set(positions)) != len(positions)
                or (positions and value['action'] != 'retire_with_history')):
            raise ValueError(DENIAL)
        return IdentityManagementIntent(1, _uuid(value['request_id']), actor, channel, _uuid(inbox_id),
            _uuid(value['chat_id']), value['revision'], value['action'], float(created), float(expires),
            name, tuple(sorted(sessions)), tuple(sorted(positions)))
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError):
        raise ValueError(DENIAL) from None


def intent_mapping(intent):
    """The canonical persistence representation contains no credentials or bodies."""
    value = asdict(intent)
    value['session_ids'] = list(intent.session_ids)
    value['positions'] = list(intent.positions)
    return value


def encode_identity_intent(intent):
    return json.dumps(intent_mapping(intent), sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def intent_digest(intent):
    return hashlib.sha256(encode_identity_intent(intent).encode()).hexdigest()


def require_fresh_intent(intent, instant):
    """Execution owner calls this inside its transaction; old receipts remain readable."""
    if (isinstance(instant, bool) or not isinstance(instant, (int, float)) or not math.isfinite(instant)
            or not intent.created_at <= instant < intent.expires_at):
        raise ValueError(DENIAL)
