"""Explicit ordinary federation payloads; foreign cursors never cross the wire."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.agentbus_federation_wire_service import MAX_FRAME_BYTES, canonical


class OriginParent(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    origin_instance_id: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')
    origin_message_id: str = Field(min_length=1, max_length=128)
    destination_instance_id: str = Field(pattern=r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')


class OrdinaryPayload(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[1]
    message: dict[str, Any]
    parent: OriginParent | None

    @model_validator(mode='after')
    def validate_content(self) -> OrdinaryPayload:
        from src.agentbus_service import SendMessage

        if type(self.version) is not int or self.model_fields_set != {'version', 'message', 'parent'}:
            raise ValueError('ordinary payload fields are not canonical')
        if set(self.message) != set(SendMessage.model_fields):
            raise ValueError('ordinary message requires its complete protocol2 shape')
        message = SendMessage(**self.message)
        if message.reply_to_cursor is not None or message.thread_ts is not None:
            raise ValueError('foreign local ancestry cannot cross ordinary federation')
        return self


def decode_ordinary(text: str) -> OrdinaryPayload:
    if not isinstance(text, str) or len(text.encode()) > MAX_FRAME_BYTES:
        raise ValueError('ordinary payload exceeds limit')
    try:
        raw = json.loads(text)
        if not isinstance(raw, dict) or type(raw.get('version')) is not int:
            raise ValueError('invalid ordinary payload version')
        if raw.get('parent') is not None:
            OriginParent(**raw['parent'])
        payload = OrdinaryPayload(**raw)
        if canonical(payload.model_dump()) != text:
            raise ValueError('ordinary payload is not canonical')
        return payload
    except (RecursionError, TypeError, UnicodeError) as exc:
        raise ValueError('invalid ordinary payload') from exc


def encode_ordinary(message, parent: dict | None) -> str:
    from src.agentbus_service import SendMessage

    content = message.model_dump(include=set(SendMessage.model_fields))
    content['thread_ts'], content['reply_to_cursor'] = None, None
    identity = OriginParent(**parent) if parent is not None else None
    payload = OrdinaryPayload(version=1, message=content, parent=identity)
    text = canonical(payload.model_dump())
    decode_ordinary(text)
    return text


def outgoing_parent(store, channel: str, message, origin: str, destination: str) -> dict | None:
    from src.agentbus_reply_policy_service import validate_reply

    if message.reply_to_cursor is None:
        if message.thread_ts is not None:
            raise ValueError('ordinary federation thread requires a committed parent cursor')
        return None
    validate_reply(store, channel, message)
    row = store._db.execute('''SELECT p.*,f.payload FROM federation_inbox_projection p
        JOIN federated_messages f USING(origin_instance_id,origin_message_id,destination_instance_id)
        WHERE p.cursor=? AND p.origin_instance_id=? AND p.destination_instance_id=?''',
        (message.reply_to_cursor, destination, origin)).fetchone()
    if row is None or hashlib.sha256(row['payload'].encode()).hexdigest() != row['payload_hash']:
        raise ValueError('ordinary parent requires matching committed origin projection')
    decode_ordinary(row['payload'])
    return {name: row[name] for name in OriginParent.model_fields}


def incoming_parent(store, channel: str, origin: str, destination: str, parent: OriginParent, message):
    from src.agentbus_federation_inbox_service import InboxProjectionError

    if parent.origin_instance_id != destination or parent.destination_instance_id != origin:
        raise ValueError('ordinary parent is outside the exact origin/destination pair')
    row = store._db.execute('''SELECT o.local_cursor,o.federation_frame_hash,b.payload,b.payload_hash
        FROM local_delivery_obligations o JOIN bridge_obligations b
        ON b.origin_message_id=o.request_id AND b.origin_instance_id=? AND b.destination_instance_id=?
        WHERE o.channel=? AND o.request_id=?''',
        (destination, origin, channel, parent.origin_message_id)).fetchone()
    if (row is None or row['federation_frame_hash'] is None
            or row['federation_frame_hash'] != row['payload_hash']
            or hashlib.sha256(row['payload'].encode()).hexdigest() != row['payload_hash']):
        raise ValueError('ordinary parent has no exact local accepted frame binding')
    frame = json.loads(row['payload'])
    if frame.get('agentbus_federation') != 3 or frame.get('kind') != 'message':
        raise ValueError('ordinary parent did not opt in to the versioned contract')
    decode_ordinary(frame['payload'])
    local = store.message(channel, row['local_cursor'])
    if local is None or message.audience != 'direct' or not store.participation.same_chat(local.sender, message.recipient):
        raise ValueError('ordinary reply is not addressed to the local parent owner')
    reference = local.thread_ts or local.slack_ts
    if not reference:
        raise InboxProjectionError('ordinary parent awaits an actual provider observation')
    return local.cursor, reference


def submission_thread(store, channel: str, message_id: str, frame_hash: str) -> str | None:
    row = store._db.execute('''SELECT m.thread_ts FROM local_delivery_obligations o
        JOIN messages m ON m.cursor=o.local_cursor
        WHERE o.channel=? AND o.request_id=? AND o.federation_frame_hash=?''',
        (channel, message_id, frame_hash)).fetchone()
    if row is None:
        raise ValueError('ordinary submission has no exact accepted local cursor')
    return row['thread_ts']
