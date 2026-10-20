"""Federation application bridge and its outbound provider port."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import time
from decimal import Decimal
from typing import Callable, Protocol

from src.agentbus_federation_directory_service import DirectorySynchronizer, decode_directory
from src.agentbus_federation_inbox_service import InboxProjectionError
from src.agentbus_federation_ordinary_service import decode_ordinary, encode_ordinary
from src.agentbus_federation_service import CustodyCapacityError, DirectoryCapacityError, FederationStore
from src.agentbus_federation_wire_service import (
    MAX_FRAME_BYTES,
    Binding,
    Identity,
    canonical,
    decode_frame,
    encode_frame,
)
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


class ProviderCarrierRejected(Exception):
    """A reported provider success did not preserve the signed carrier."""

    def __init__(self, reason: str, provider_ref: str):
        if reason not in {'missing_echo', 'changed_echo', 'truncation_warning'}:
            raise ValueError('invalid provider carrier failure')
        self.reason, self.provider_ref = reason, provider_ref
        super().__init__('federation provider carrier rejected: ' + reason)


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
    except CustodyCapacityError:
        raise EnqueueRejected(503, 'Federation custody capacity requires operator archival', {'Retry-After': '60'}) from None
    except (ValueError, KeyError):
        raise EnqueueRejected(422, 'Federation message or route is invalid') from None


def _recovery_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError('invalid federation recovery budget')
    return limit


class FederationBridge:
    def __init__(self, store: FederationStore, identity: Identity,
                 bindings: dict[str, Binding], provider: FederationProvider,
                 *, now: Callable[[], float] = time.time, max_attempts: int = 8, history_limit: int = 100):
        self.history_limit = _recovery_limit(history_limit)
        if not 1 <= max_attempts <= 32:
            raise ValueError('invalid federation attempt budget')
        self.store, self.identity, self.bindings, self.provider = store, identity, dict(bindings), provider
        self.now, self.max_attempts = now, max_attempts
        self._recovery_complete = False
        self._recovery_attempts: dict[tuple[str, str, str], int] | None = None
        self.directory: DirectorySynchronizer | None = None
        self.project_submission: Callable[[str, str, str], bool] | None = None
        self.ordinary_channel: str | None = None
        self.submission_thread: Callable[[str, str], str | None] | None = None
        self.project_ordinary: Callable[[str, str, str, str, str | None], None] | None = None
        self.project_message: Callable[[str, str, str, str, str | None], None] | None = None
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

    def enqueue_ordinary(self, message_id: str, destination: str, message, parent: dict | None) -> None:
        if destination not in self.bindings or self.bindings[destination].ordinary_version != 3:
            raise ValueError('ordinary peer version is not operator-approved')
        payload = encode_ordinary(message, parent)
        self._message(canonical(decode_ordinary(payload).message), self.identity.instance_id, destination)
        frame = encode_frame(self.identity, 'message', message_id, destination, payload, version=3)
        self.store.accept(origin_instance_id=self.identity.instance_id, origin_message_id=message_id,
                          destination_instance_id=destination, payload=frame, local_payload=payload)

    def directory_now(self) -> float:
        return self.now()

    def enqueue_directory(self, destination: str, payload: str) -> None:
        if destination not in self.bindings or self.bindings[destination].directory_version != 2:
            raise ValueError('directory peer version is not operator-approved')
        decode_directory(payload, self.identity.route_prefix)
        message = 'directory-' + hashlib.sha256(payload.encode()).hexdigest()
        frame = encode_frame(self.identity, 'directory', message, destination, payload)
        self.store.accept(origin_instance_id=self.identity.instance_id, origin_message_id=message,
                          destination_instance_id=destination, payload=frame, local_payload=payload, kind='directory')

    async def submit_once(self, *, retry_uncertain: bool = True,
                          covered_attempts: dict[tuple[str, str, str], int] | None = None) -> None:
        for row in self.store.pending(now=self.now(), max_attempts=self.max_attempts,
                                      unattempted_only=not retry_uncertain):
            key = (row['origin_instance_id'], row['origin_message_id'], row['destination_instance_id'])
            if row['attempts'] and covered_attempts is not None and covered_attempts.get(key) != row['attempts']:
                continue
            if key[0] != self.identity.instance_id:
                continue
            digest = hashlib.sha256(row['payload'].encode()).hexdigest()
            if digest != row['payload_hash']:
                raise ValueError('retained carrier content does not match its custody hash')
            thread = None
            frame = json.loads(row['payload'])
            if frame.get('agentbus_federation') == 3 and decode_ordinary(frame['payload']).parent is not None:
                if self.submission_thread is None or not callable(getattr(self.provider, 'publish_in_thread', None)):
                    raise ValueError('ordinary threaded provider port is unavailable')
                thread = self.submission_thread(row['origin_message_id'], digest)
                if not thread:
                    raise InboxProjectionError('ordinary submission awaits its actual local parent reference')
            delay = min(600, 60 * 2 ** row['attempts'])
            if not self.store.reserve_submission(*key, now=self.now(), delay=delay, max_attempts=self.max_attempts):
                continue
            try:
                reference = (await getattr(self.provider, 'publish_in_thread')(row['payload'], thread)
                             if thread is not None else await self.provider.publish(row['payload']))
            except ProviderCarrierRejected as exc:
                self.store.block_submission(*key, reason=exc.reason, provider_ref=exc.provider_ref)
            except ProviderUnavailable as exc:
                self.store.defer_submission(*key, until=self.now() + max(delay, exc.retry_after))
            except (TimeoutError, OSError):
                pass
            else:
                self.store.record_submission(*key, reference)
                if self.project_submission is not None:
                    self.project_submission(row['origin_message_id'], digest, reference)

    def _observe_origin(self, event: dict) -> bool:
        project = self.project_submission
        if project is None or event.get('publisher_id') != self.identity.publisher_id:
            return False
        text, reference = event.get('text'), event.get('ts')
        if not isinstance(text, str) or len(text.encode()) > MAX_FRAME_BYTES:
            raise ValueError('invalid own carrier observation')
        try:
            frame = json.loads(text)
        except RecursionError as exc:
            raise ValueError('own carrier JSON exceeds decoder depth') from exc
        if (not isinstance(frame, dict) or frame.get('origin_instance_id') != self.identity.instance_id
                or frame.get('kind') != 'message'):
            return False
        message, destination = frame.get('origin_message_id'), frame.get('destination_instance_id')
        if not isinstance(message, str) or not isinstance(destination, str):
            raise ValueError('invalid own carrier identity')
        frame = decode_frame(text, self.identity.publisher_id, destination, {self.identity.instance_id:
            Binding(self.identity.instance_id, self.identity.route_prefix, self.identity.publisher_id,
                    self.identity.private_key.public_key(), ordinary_version=3)})
        message, destination = frame['origin_message_id'], frame['destination_instance_id']
        row = self.store.obligation(self.identity.instance_id, message, destination)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if row is None:
            return False
        if row['payload'] != text or row['payload_hash'] != digest:
            raise ValueError('own carrier is not exact retained custody')
        if not isinstance(reference, str) or not re.fullmatch(r'[1-9][0-9]{0,15}\.[0-9]{1,9}', reference):
            raise ValueError('own carrier needs an actual provider reference')
        self.store.observe_submission(self.identity.instance_id, message, destination, reference)
        project(message, digest, reference)
        return True

    def receive(self, event: dict) -> None:
        if self.project_submission is not None and self._observe_origin(event):
            return
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
        if frame['kind'] == 'directory':
            if self.directory is None:
                raise ValueError('directory synchronization is not configured')
            self.directory.receive(frame)
            payload = frame['payload']
        elif frame['agentbus_federation'] == 3:
            if self.project_ordinary is None:
                raise ValueError('ordinary federation projection is not configured')
            ordinary = decode_ordinary(frame['payload'])
            self._message(canonical(ordinary.message), origin, self.identity.instance_id)
            payload = frame['payload']
            self.store.accept_destination(origin_instance_id=origin, publisher_id=event['publisher_id'],
                origin_message_id=message_id, destination_instance_id=self.identity.instance_id, payload=payload)
            self.project_ordinary(origin, message_id, self.identity.instance_id, payload, event.get('ts'))
        else:
            payload = self._message(frame['payload'], origin, self.identity.instance_id)
            self.store.accept_destination(origin_instance_id=origin, publisher_id=event['publisher_id'],
                origin_message_id=message_id, destination_instance_id=self.identity.instance_id, payload=payload)
            if self.project_message is not None:
                self.project_message(origin, message_id, self.identity.instance_id, payload, event.get('ts'))
        receipt = canonical({'origin': origin, 'message': message_id,
            'destination': self.identity.instance_id, 'payload_hash': hashlib.sha256(payload.encode()).hexdigest()})
        receipt_id = 'receipt-' + hashlib.sha256(canonical([origin, message_id, self.identity.instance_id]).encode()).hexdigest()
        signed = encode_frame(self.identity, 'receipt', receipt_id, origin, receipt)
        self.store.accept(origin_instance_id=self.identity.instance_id, origin_message_id=receipt_id,
                          destination_instance_id=origin, payload=signed, kind='receipt')

    async def recover_once(self, limit: int = 100) -> None:
        limit = _recovery_limit(limit)
        if self._recovery_complete:
            self._recovery_attempts = None
        self._recovery_complete = False
        if self._recovery_attempts is None:
            self._recovery_attempts = {
                (row['origin_instance_id'], row['origin_message_id'], row['destination_instance_id']): row['attempts']
                for row in self.store.pending(now=self.now(), max_attempts=self.max_attempts) if row['attempts']}
        reader = self.identity.instance_id + '/' + self.identity.publisher_id
        scanner = getattr(self.provider, 'scan_history', None)
        if scanner is None:
            events = await self.provider.history(self.store.checkpoint(reader), limit)
            complete = True
        else:
            events, complete = await scanner(self.store.checkpoint(reader), limit)
            if type(complete) is not bool:
                raise ValueError('invalid history coverage receipt')
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
        self._recovery_complete = complete

    async def cycle(self) -> float:
        delay = 60.0
        try:
            if self.directory is not None:
                self.directory.reconcile()
            await self.recover_once(limit=self.history_limit)
            await self.submit_once(retry_uncertain=self._recovery_complete,
                                   covered_attempts=self._recovery_attempts)
        except ProviderUnavailable as exc:
            delay = max(delay, exc.retry_after)
            LOGGER.warning('Federation provider unavailable; durable custody retained')
        except (TimeoutError, OSError, ValueError, sqlite3.Error, DirectoryCapacityError, CustodyCapacityError,
                InboxProjectionError):
            LOGGER.warning('Federation cycle incomplete; durable custody retained')
        return delay

    async def run(self) -> None:
        while True:
            await asyncio.sleep(await self.cycle())
