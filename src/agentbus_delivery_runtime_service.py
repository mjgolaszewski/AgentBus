"""One in-process transport cycle for local outbox and optional federation."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time

from fastapi import HTTPException

from src.agentbus_federation_bridge_service import ProviderCarrierRejected

LOGGER = logging.getLogger('agentbus')


class LocalDeliveryWorker:
    def __init__(self, store, poster, channel: str):
        self.store, self.poster, self.channel = store, poster, channel
        self.wake = asyncio.Event()

    async def submit_once(self) -> None:
        from src.agentbus_service import SendMessage

        deadline = time.monotonic() + 30
        for row in self.store.local_delivery.pending()[:8]:
            if time.monotonic() >= deadline:
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
            try:
                reference = await self.poster.post(message, message_id=row['request_id'])
                self.store.local_delivery.complete(self.channel, row['request_id'], reference)
            except ProviderCarrierRejected as exc:
                self.store.local_delivery.blocked(self.channel, row['request_id'], exc.reason, exc.provider_ref)
            except (HTTPException, sqlite3.Error, OSError, ValueError):
                self.store.local_delivery.uncertain(self.channel, row['request_id'])
                LOGGER.warning('Ordinary provider submission incomplete; local custody retained')

    async def run(self, bridge=None, *, connect=None) -> None:
        next_federation = 0.0
        while True:
            self.wake.clear()
            try:
                if connect is not None:
                    try:
                        bridge = await connect()
                    except RuntimeError:
                        LOGGER.warning('Slack bootstrap unavailable; local custody retained')
                        try:
                            await asyncio.wait_for(self.wake.wait(), timeout=60)
                        except TimeoutError:
                            pass
                        continue
                await self.submit_once()
            except (sqlite3.Error, OSError, ValueError):
                LOGGER.warning('Local delivery cycle incomplete; durable custody retained')
            if bridge is not None and time.monotonic() >= next_federation:
                next_federation = time.monotonic() + await bridge.cycle()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=60)
            except TimeoutError:
                pass
