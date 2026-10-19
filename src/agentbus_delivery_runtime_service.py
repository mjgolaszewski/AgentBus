"""One in-process transport cycle for local outbox and optional federation."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from datetime import datetime

from fastapi import HTTPException

from src.agentbus_federation_bridge_service import FederationBridge, ProviderCarrierRejected, ProviderUnavailable

LOGGER = logging.getLogger('agentbus')


class LocalDeliveryWorker:
    def __init__(self, store, poster, channel: str):
        self.store, self.poster, self.channel = store, poster, channel
        self.wake = asyncio.Event()
        self.cycle_attempts = 0
        self.trusted_bot_id: str | None = None
        self.federation: FederationBridge | None = None

    async def recover_once(self, rows: list[dict]) -> set[str]:
        if not self.trusted_bot_id or not callable(getattr(self.poster, 'observed_messages', None)):
            self.store.local_delivery.defer_recovery(rows)
            return set()
        try:
            after = max(0, min(datetime.fromisoformat(row['received_at']).timestamp() for row in rows) - 1)
            events = await self.poster.observed_messages(f'{after:.6f}', 100)
            if len(events) > 100:
                raise ValueError('local provider history exceeds recovery budget')
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError('invalid local history event')
                publisher = event.get('publisher_id')
                if not isinstance(publisher, str):
                    continue
                if (self.federation is not None and self.federation.ordinary_channel == self.channel
                        and publisher == self.federation.identity.publisher_id
                        and isinstance(event.get('text'), str)
                        and event['text'].startswith('{"agentbus_federation":')):
                    if self.federation._observe_origin(event):
                        continue
                publisher_parts = publisher.split('/')
                if len(publisher_parts) != 3 or publisher_parts[1:] != [self.trusted_bot_id, self.channel]:
                    continue
                self.store.local_delivery.accept_event_echo(self.channel, event['ts'], event.get('text'),
                    event.get('blocks'), event.get('thread_ts'))
            self.store.local_delivery.defer_recovery([row for row in rows
                if row['state'] in {'blocked', 'federated'} or row['attempts'] >= 8])
            return {row['request_id'] for row in rows}
        except (ProviderUnavailable, sqlite3.Error, OSError, ValueError, KeyError, TypeError):
            self.store.local_delivery.defer_recovery(rows)
            LOGGER.warning('Local provider history incomplete; uncertain custody retained')
            return set()

    async def submit_once(self, *, budget=8, deadline=None) -> int:
        from src.agentbus_service import SendMessage

        self.cycle_attempts = 0
        self.store.local_delivery.settle_expired(self.channel)
        pending = self.store.local_delivery.pending(channel=self.channel)
        uncertain = self.store.local_delivery.recovery_pending(self.channel)
        if self.federation is not None and self.federation.ordinary_channel == self.channel:
            uncertain = (uncertain + self.store.local_delivery.federated_recovery_pending(
                self.channel, self.federation.identity.instance_id))[:100]
        covered = await self.recover_once(uncertain) if uncertain else set()
        deadline, attempted = deadline or time.monotonic() + 30, 0
        # A successful parent can make a child ready during this same cycle.
        # Refresh only after progress, within the original shared attempt budget.
        for _ in range(budget):
            before = attempted
            for row in pending:
                if row['attempts'] > 0 and row['request_id'] not in covered:
                    continue
                if time.monotonic() >= deadline or attempted >= budget:
                    break
                message = SendMessage.model_validate_json(row['payload'])
                if message.reply_to_cursor is not None:
                    parent = self.store.message(self.channel, message.reply_to_cursor)
                    if parent is None or not (parent.thread_ts or parent.slack_ts):
                        continue
                    message.thread_ts = parent.thread_ts or parent.slack_ts
                    with self.store._lock, self.store._db:
                        self.store._db.execute('UPDATE messages SET payload=?,thread_ts=? WHERE cursor=?',
                                              (message.model_dump_json(), message.thread_ts, row['local_cursor']))
                if not self.store.local_delivery.reserve(self.channel, row['request_id']):
                    continue
                attempted += 1
                self.cycle_attempts = attempted
                try:
                    reference = await self.poster.post(message, message_id=row['request_id'])
                    self.store.local_delivery.complete(self.channel, row['request_id'], reference)
                except ProviderCarrierRejected as exc:
                    self.store.local_delivery.blocked(self.channel, row['request_id'], exc.reason, exc.provider_ref)
                except (HTTPException, sqlite3.Error, OSError, ValueError):
                    self.store.local_delivery.uncertain(self.channel, row['request_id'])
                    LOGGER.warning('Ordinary provider submission incomplete; local custody retained')
            if attempted == before or attempted >= budget or time.monotonic() >= deadline:
                break
            pending = self.store.local_delivery.pending(channel=self.channel)

        return attempted

    async def run(self, bridge=None, *, connect=None) -> None:
        await run_delivery(self, bridge, connect=connect)


async def run_delivery(worker, bridge=None, *, connect=None):
    next_federation = 0.0
    observations = getattr(worker, 'observations', None)
    while True:
        worker.wake.clear()
        if observations is not None:
            observations.mark('delivery', 'running')
        try:
            if worker.store.lifecycle.enabled:
                worker.store.lifecycle.rotate_epochs()
                channels = [item.channel for item in worker.workers] if hasattr(worker, 'workers') else [worker.channel]
                for channel in channels:
                    worker.store.lifecycle.settle(channel=channel)
                storage = worker.store.retention.storage()
                if storage['accounted_bytes'] * 5 >= storage['limit_bytes'] * 4:
                    worker.store.retention.checkpoint()
            if connect is not None:
                try:
                    bridge = await connect()
                except RuntimeError:
                    LOGGER.warning('Slack bootstrap unavailable; local custody retained')
                    if observations is not None:
                        observations.mark('delivery', 'backoff', error='slack_bootstrap_unavailable')
                    try:
                        await asyncio.wait_for(worker.wake.wait(), timeout=60)
                    except TimeoutError:
                        pass
                    continue
            await worker.submit_once()
            if observations is not None:
                if getattr(worker, 'cycle_incomplete', False):
                    observations.mark('delivery', 'degraded', error='channel_cycle_incomplete')
                else:
                    observations.mark('delivery', 'waiting', completed=True)
        except (sqlite3.Error, OSError, ValueError):
            LOGGER.warning('Local delivery cycle incomplete; durable custody retained')
            if observations is not None:
                observations.mark('delivery', 'degraded', error='delivery_cycle_incomplete')
        if observations is not None and bridge is None:
            observations.mark('federation', 'disabled')
        if bridge is not None and time.monotonic() >= next_federation:
            if observations is not None:
                observations.mark('federation', 'running')
            try:
                next_federation = time.monotonic() + await bridge.cycle()
            except Exception:
                if observations is not None:
                    observations.mark('federation', 'failed', error='federation_cycle_failed')
                raise
            if observations is not None:
                observations.mark('federation', 'waiting', completed=True)
        try:
            await asyncio.wait_for(worker.wake.wait(), timeout=60)
        except TimeoutError:
            pass


class DeliveryCoordinator:
    """One bounded loop rotates channel-bound workers; one bridge cycles once."""
    def __init__(self,store,posters):
        from src.agentbus_worker_status_service import WorkerObservations
        self.store=store
        self.workers=[LocalDeliveryWorker(store,poster,channel) for channel,poster in posters.items()]
        self.wake=asyncio.Event()
        self.offset=0
        self.observations = WorkerObservations()

    def bind_transport(self,bot_id,bridge):
        for worker in self.workers:
            worker.trusted_bot_id=bot_id
            worker.federation=bridge if bridge is not None and bridge.channel==worker.channel else None

    async def submit_once(self):
        self.cycle_incomplete = False
        quota,deadline=8,time.monotonic()+30
        workers=self.workers[self.offset:]+self.workers[:self.offset]
        self.offset=(self.offset+1)%len(workers)
        for worker in workers:
            if quota<=0 or time.monotonic()>=deadline:
                break
            try:
                quota-=await worker.submit_once(budget=quota,deadline=deadline)
            except (sqlite3.Error,OSError,ValueError):
                self.cycle_incomplete = True
                quota -= worker.cycle_attempts
                LOGGER.warning('Channel cycle incomplete; retained custody remains channel-bound')

    async def run(self,bridge=None,*,connect=None):
        self.observations.mark('task', 'running')
        try:
            await run_delivery(self,bridge,connect=connect)
        except asyncio.CancelledError:
            self.observations.finish_task()
            raise
        except Exception:
            self.observations.finish_task(failed=True)
            raise
        else:
            self.observations.finish_task()
