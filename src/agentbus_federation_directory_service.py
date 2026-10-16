"""Opt-in, service-owned directory synchronization on the existing bridge."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, StrictInt, model_validator

from src.agentbus_federation_service import MAX_DIRECTORY_REVISION, FederationStore
from src.agentbus_federation_wire_service import Binding, Identity, canonical
from src.agentbus_message_assurance_service import route_parts


class DirectoryPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    directory: Literal[1]
    operation: Literal['snapshot', 'delta', 'request']
    revision: StrictInt | None = None
    entries: list[dict[str, Any]] | None = None
    route: str | None = None
    state: Literal['current', 'withdrawn'] | None = None
    metadata: dict[str, Any] | None = None
    after_revision: StrictInt | None = None
    request_id: str | None = None
    response_to: str | None = None

    @model_validator(mode='after')
    def validate_operation(self) -> DirectoryPayload:
        required = {
            'snapshot': {'directory', 'operation', 'revision', 'entries'},
            'delta': {'directory', 'operation', 'revision', 'route', 'state', 'metadata'},
            'request': {'directory', 'operation', 'after_revision', 'request_id'},
        }[self.operation]
        optional = {'response_to'} if self.operation == 'snapshot' and self.response_to is not None else set()
        if self.model_fields_set != required | optional:
            raise ValueError('directory operation fields are not canonical')
        if self.operation == 'request':
            if (self.after_revision is None or not 0 <= self.after_revision <= MAX_DIRECTORY_REVISION
                    or self.request_id is None):
                raise ValueError('invalid directory request')
            UUID(self.request_id)
        elif self.revision is None or not 1 <= self.revision <= MAX_DIRECTORY_REVISION:
            raise ValueError('invalid directory revision')
        if self.operation == 'snapshot' and (self.entries is None or len(self.entries) > 256):
            raise ValueError('directory snapshot exceeds limit')
        if self.response_to is not None:
            UUID(self.response_to)
        return self


def decode_directory(text: str, prefix: str) -> DirectoryPayload:
    """Validate one canonical payload within the verified origin namespace."""
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict) or type(raw.get('directory')) is not int:
            raise ValueError('invalid directory version')
        payload = DirectoryPayload.model_validate(raw)
        if canonical(raw) != text:
            raise ValueError('directory payload is not canonical')
        routes = [entry.get('route') for entry in payload.entries or []]
        if payload.operation == 'delta':
            routes.append(payload.route)
        for route in routes:
            parsed = route_parts(route) if isinstance(route, str) else None
            if parsed is None or parsed[0] != prefix:
                raise ValueError('directory route is outside its operator origin binding')
        return payload
    except (RecursionError, TypeError, UnicodeError) as exc:
        raise ValueError('invalid directory payload') from exc


class DirectoryBridge(Protocol):
    identity: Identity
    bindings: dict[str, Binding]
    store: FederationStore

    def enqueue_directory(self, destination: str, payload: str) -> None: ...
    def directory_now(self) -> float: ...


class ParticipationRoster(Protocol):
    def roster(self, *, include_stopped: bool = False) -> list[dict]: ...


class DirectorySynchronizer:
    def __init__(self, bridge: DirectoryBridge, participation: ParticipationRoster,
                 *, share_chat_ids: list[str], peers: list[str]):
        if len(share_chat_ids) > 256 or len(set(share_chat_ids)) != len(share_chat_ids):
            raise ValueError('invalid directory sharing allowlist')
        for chat in share_chat_ids:
            UUID(chat)
        if len(peers) > 64 or len(set(peers)) != len(peers):
            raise ValueError('invalid directory peers')
        if any(peer == bridge.identity.instance_id or peer not in bridge.bindings
               or bridge.bindings[peer].directory_version != 2 for peer in peers):
            raise ValueError('directory peer version is not operator-approved')
        self.bridge, self.participation = bridge, participation
        self.shared, self.peers = set(share_chat_ids), tuple(peers)

    def _snapshot(self, response_to: str | None = None) -> dict:
        selected: dict[str, dict] = {}
        for row in self.participation.roster(include_stopped=True):
            chat = row['chat_id']
            if chat not in self.shared:
                continue
            if chat not in selected or row['last_client_contact_at'] > selected[chat]['last_client_contact_at']:
                selected[chat] = row
        entries = []
        for chat, row in selected.items():
            # Overdue contact is not registration withdrawal or running state.
            if row['state'] not in {'active_compliant', 'control_pending', 'overdue'}:
                continue
            parsed = route_parts(row['route'])
            if parsed is None or parsed[0] != self.bridge.identity.route_prefix:
                raise ValueError('local directory route is outside its origin binding')
            entries.append({'route': row['route'], 'metadata': {
                'agent_id': chat, 'protocol': 2, 'registration_state': 'active'}})
        revision, entries = self.bridge.store.local_directory_snapshot(self.bridge.identity.instance_id, entries)
        result = {'directory': 1, 'operation': 'snapshot', 'revision': revision, 'entries': entries}
        if response_to is not None:
            result['response_to'] = response_to
        return result

    def _send(self, destination: str, payload: dict) -> None:
        text = canonical(payload)
        decode_directory(text, self.bridge.identity.route_prefix)
        self.bridge.enqueue_directory(destination, text)

    def reconcile(self) -> None:
        snapshot = self._snapshot()
        for peer in self.peers:
            self._send(peer, snapshot)
            stream = self.bridge.store.directory_revision(peer)
            if stream['revision'] and not stream['gap_revision']:
                continue
            request = self.bridge.store.directory_request(peer, stream['revision'],
                                                           stream['gap_revision'], str(uuid4()))
            self._send(peer, {'directory': 1, 'operation': 'request',
                              'after_revision': stream['revision'], 'request_id': request})

    def receive(self, frame: dict) -> None:
        origin = frame['origin_instance_id']
        if origin not in self.peers:
            raise ValueError('directory origin is not operator-approved')
        text = frame['payload']
        expected = 'directory-' + hashlib.sha256(text.encode()).hexdigest()
        if frame['origin_message_id'] != expected:
            raise ValueError('directory identity must bind canonical payload bytes')
        payload = decode_directory(text, self.bridge.bindings[origin].route_prefix)
        if payload.operation == 'snapshot':
            assert payload.revision is not None and payload.entries is not None
            self.bridge.store.directory_snapshot(origin_instance_id=origin,
                revision=payload.revision, entries=payload.entries)
        elif payload.operation == 'delta':
            assert payload.revision is not None and payload.route is not None
            assert payload.state is not None and payload.metadata is not None
            self.bridge.store.apply_directory_delta(origin_instance_id=origin,
                route=payload.route, revision=payload.revision,
                state=payload.state, metadata=payload.metadata)
        else:
            assert payload.request_id is not None
            if not self.bridge.store.reserve_directory_response(origin, payload.request_id, self.bridge.directory_now()):
                raise ValueError('directory request rate exceeded; no custody receipt')
            self._send(origin, self._snapshot(payload.request_id))
