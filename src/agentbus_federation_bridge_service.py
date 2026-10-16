"""Federation application bridge and its outbound provider port."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from decimal import Decimal
from typing import Callable, Protocol

from src.agentbus_federation_service import FederationStore
from src.agentbus_federation_wire_service import Binding, Identity, canonical, decode_frame, encode_frame
from src.agentbus_message_assurance_service import route_parts

LOGGER = logging.getLogger('agentbus')


class FederationProvider(Protocol):
    async def publish(self, text: str) -> str:
        """Submit a durable frame; a provider receipt is not destination custody."""
        ...

    async def history(self, after: str, limit: int) -> list[dict]:
        """Return complete bounded history, oldest first, or fail without progress."""
        ...


class ProviderUnavailable(Exception):
    def __init__(self, retry_after: float = 0):
        self.retry_after = retry_after
        super().__init__('federation provider unavailable; submission remains uncertain')


class EnqueueRejected(Exception):
    def __init__(self, status: int, reason: str, headers: dict | None = None):
        self.status, self.headers = status, headers
        super().__init__(reason)


def enqueue_authenticated(bridge, store, channel, message_id, destination, payload, session_token) -> None:
    from src.agentbus_participation_store_service import StoppedMessageSession
    from src.agentbus_send_policy_service import SessionAuthorityError, validate_new_send
    from src.agentbus_send_rate_service import SendRateExceeded
    from src.agentbus_service import SendMessage

    try:
        message = SendMessage.model_validate(payload)
        rate_key = validate_new_send(store, channel, message, session_token)
        if message._sender_assurance != 'session':
            raise PermissionError('Federation enqueue requires a joined session')
        if store.federation.obligation(bridge.identity.instance_id, message_id, destination) is None:
            store.send_rate.reserve(rate_key)
        bridge.enqueue(message_id, destination, canonical(payload))
    except StoppedMessageSession:
        raise EnqueueRejected(423, 'Participation session is stopped') from None
    except (PermissionError, SessionAuthorityError):
        raise EnqueueRejected(403, 'Federation enqueue requires the matching joined session') from None
    except SendRateExceeded as exc:
        raise EnqueueRejected(429, 'Federation send rate exceeded', {'Retry-After': str(exc.retry_after)}) from None
    except (ValueError, KeyError):
        raise EnqueueRejected(422, 'Federation message or route is invalid') from None


class FederationBridge:
    def __init__(self, store: FederationStore, identity: Identity,
                 bindings: dict[str, Binding], provider: FederationProvider,
                 *, now: Callable[[], float] = time.time, max_attempts: int = 8):
        if not 1 <= max_attempts <= 32:
            raise ValueError('invalid federation attempt budget')
        self.store, self.identity, self.bindings, self.provider = store, identity, dict(bindings), provider
        self.now, self.max_attempts = now, max_attempts
        publishers = {identity.publisher_id: identity.instance_id}
        prefixes = {identity.route_prefix: identity.instance_id}
        for origin, binding in self.bindings.items():
            if origin != binding.instance_id:
                raise ValueError('conflicting federation binding identity')
            if (publishers.get(binding.publisher_id, origin) != origin
                    or prefixes.get(binding.route_prefix, origin) != origin):
                raise ValueError('independent federation origins require distinct publisher and route bindings')
            publishers[binding.publisher_id], prefixes[binding.route_prefix] = origin, origin
            self.store.bind_origin(origin_instance_id=origin, publisher_id=binding.publisher_id)

    def _message(self, payload: str, origin: str, destination: str) -> str:
        from src.agentbus_service import SendMessage

        message = SendMessage.model_validate(json.loads(payload))
        sender_prefix = self.identity.route_prefix if origin == self.identity.instance_id else self.bindings[origin].route_prefix
        recipient_prefix = self.identity.route_prefix if destination == self.identity.instance_id else self.bindings[destination].route_prefix
        sender, recipient = route_parts(message.sender), route_parts(message.recipient)
        if (message.audience not in {'direct', 'informational'} or
                sender is None or sender[0] != sender_prefix or
                recipient is None or recipient[0] != recipient_prefix):
            raise ValueError('federation route is outside its operator binding')
        return canonical(message.model_dump(include=set(SendMessage.model_fields)))

    def enqueue(self, message_id: str, destination: str, payload: str) -> None:
        if destination not in self.bindings or destination == self.identity.instance_id:
            raise ValueError('unknown federation destination')
        local = self._message(payload, self.identity.instance_id, destination)
        frame = encode_frame(self.identity, 'message', message_id, destination, local)
        self.store.accept(origin_instance_id=self.identity.instance_id, origin_message_id=message_id,
                          destination_instance_id=destination, payload=frame, local_payload=local)

    async def submit_once(self) -> None:
        for row in self.store.pending(now=self.now(), max_attempts=self.max_attempts):
            key = (row['origin_instance_id'], row['origin_message_id'], row['destination_instance_id'])
            if key[0] != self.identity.instance_id:
                continue
            delay = min(600, 60 * 2 ** row['attempts'])
            if not self.store.reserve_submission(*key, now=self.now(), delay=delay, max_attempts=self.max_attempts):
                continue
            try:
                reference = await self.provider.publish(row['payload'])
            except ProviderUnavailable as exc:
                self.store.defer_submission(*key, until=self.now() + max(delay, exc.retry_after))
            except (TimeoutError, OSError):
                pass
            else:
                self.store.record_submission(*key, reference)

    def receive(self, event: dict) -> None:
        frame = decode_frame(event['text'], event['publisher_id'], self.identity.instance_id, self.bindings)
        origin, message_id = frame['origin_instance_id'], frame['origin_message_id']
        if frame['kind'] == 'receipt':
            receipt = json.loads(frame['payload'])
            if set(receipt) != {'origin', 'message', 'destination', 'payload_hash'}:
                raise ValueError('invalid destination receipt')
            if receipt['origin'] != self.identity.instance_id or receipt['destination'] != origin:
                raise ValueError('conflicting destination receipt identity')
            row = self.store.obligation(receipt['origin'], receipt['message'], origin)
            if row is None:
                raise ValueError('unknown destination receipt')
            sent = json.loads(row['payload'])
            if receipt['payload_hash'] != hashlib.sha256(sent['payload'].encode()).hexdigest():
                raise ValueError('conflicting destination receipt payload')
            if row['state'] not in {'destination_accepted', 'acknowledged'}:
                self.store.transition(receipt['origin'], receipt['message'], origin, 'destination_accepted')
            return
        payload = self._message(frame['payload'], origin, self.identity.instance_id)
        self.store.accept_destination(origin_instance_id=origin, publisher_id=event['publisher_id'],
            origin_message_id=message_id, destination_instance_id=self.identity.instance_id, payload=payload)
        receipt = canonical({'origin': origin, 'message': message_id,
            'destination': self.identity.instance_id, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})
        receipt_id = 'receipt-' + hashlib.sha256(canonical([origin, message_id, self.identity.instance_id]).encode()).hexdigest()
        signed = encode_frame(self.identity, 'receipt', receipt_id, origin, receipt)
        self.store.accept(origin_instance_id=self.identity.instance_id, origin_message_id=receipt_id,
                          destination_instance_id=origin, payload=signed, kind='receipt')

    async def recover_once(self, limit: int = 100) -> None:
        if not 1 <= limit <= 1000:
            raise ValueError('invalid federation recovery budget')
        reader = self.identity.instance_id + '/' + self.identity.publisher_id
        events = await self.provider.history(self.store.checkpoint(reader), limit)
        if len(events) > limit:
            raise ValueError('provider history exceeds recovery budget')
        previous = Decimal(self.store.checkpoint(reader))
        for event in events:
            stamp = Decimal(event['ts'])
            if not stamp.is_finite() or stamp <= previous:
                raise ValueError('provider history is not strictly ordered')
            previous = stamp
        for event in events:
            try:
                self.receive(event)
            except (ValueError, KeyError, TypeError):
                # Untrusted or irrelevant provider text cannot stop recovery.
                LOGGER.info('Rejected non-admitted federation history event')
        if events:
            self.store.advance_checkpoint(reader, events[-1]['ts'])

    async def run(self) -> None:
        while True:
            delay = 60.0
            try:
                await self.recover_once()
                await self.submit_once()
            except ProviderUnavailable as exc:
                delay = max(delay, exc.retry_after)
                LOGGER.warning('Federation provider unavailable; durable custody retained')
            except (TimeoutError, OSError, ValueError, sqlite3.Error):
                LOGGER.warning('Federation cycle incomplete; durable custody retained')
            await asyncio.sleep(delay)
