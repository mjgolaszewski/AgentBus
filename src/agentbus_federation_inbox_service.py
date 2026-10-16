"""Project retained authenticated custody into a protocol-2 local notification."""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.agentbus_service import MessageStore


class InboxProjectionError(RuntimeError):
    """Projection needs owner recovery; no receipt or checkpoint may hide it."""


def project_message(store: MessageStore, channel: str, origin: str, message_id: str,
                    destination: str, payload: str, provider_ts: str | None) -> None:
    from src.agentbus_service import SendMessage

    if not isinstance(provider_ts, str) or not re.fullmatch(r'[1-9][0-9]{0,15}\.[0-9]{1,9}', provider_ts):
        raise InboxProjectionError('federation projection requires an actual provider timestamp')
    digest = hashlib.sha256(payload.encode()).hexdigest()
    key = (origin, message_id, destination)
    with store._lock, store._db:
        store.federation._custody_writer()
        custody = store._db.execute('''SELECT payload_hash FROM federated_messages
            WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?''', key).fetchone()
        if custody is None or custody['payload_hash'] != digest:
            raise InboxProjectionError('federation projection requires matching authenticated custody')
        existing = store._db.execute('''SELECT cursor, payload_hash FROM federation_inbox_projection
            WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?''', key).fetchone()
        if existing is not None:
            if existing['payload_hash'] != digest or store.message(channel, existing['cursor']) is None:
                raise InboxProjectionError('conflicting federation inbox projection')
            return
        store.federation._custody_capacity('federation_inbox_projection')
        if store.message_by_ts(channel, provider_ts) is not None:
            raise InboxProjectionError('federation provider timestamp already belongs to a local message')
        message = SendMessage.model_validate_json(payload)
        # Remote ancestry remains in signed custody; protocol-2 local references
        # cannot acquire meaning merely by sharing an integer or timestamp.
        message.thread_ts, message.reply_to_cursor = None, None
        projected = store._append(channel, provider_ts, message)
        store._db.execute('''INSERT INTO federation_inbox_projection
            (origin_instance_id, origin_message_id, destination_instance_id, cursor, payload_hash)
            VALUES (?, ?, ?, ?, ?)''', (*key, projected.cursor, digest))
