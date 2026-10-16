"""Single transport task; explicit offline bootstrap never trusts an unknown bot."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress

import httpx

from src.agentbus_delivery_runtime_service import LocalDeliveryWorker
from src.agentbus_federation_runtime_service import load_bridge
from src.agentbus_slack_socket_service import SlackReceiver

LOGGER = logging.getLogger('agentbus')


class DeliveryTransport:
    def __init__(self, app, store, settings, client, poster, socket_factory):
        self.app, self.store, self.settings = app, store, settings
        self.client, self.poster, self.socket_factory = client, poster, socket_factory
        self.socket = None
        self.receiver = None
        self.bridge = None
        self.connected = False
        self.next_bootstrap = 0.0

    async def connect(self):
        if self.connected:
            return self.bridge
        if time.monotonic() < self.next_bootstrap:
            raise RuntimeError('Slack bootstrap backoff active')
        self.next_bootstrap = time.monotonic() + 60
        bot_id = await self.poster.authenticated_bot_id()
        settings = self.settings
        bridge = (load_bridge(settings.federation_config, self.store, self.client,
                              settings.slack_bot_token, bot_id)
                  if settings.federation_config is not None else None)
        self.receiver = SlackReceiver(self.store, settings.slack_channel, bot_id, settings, bridge)
        self.socket = self.socket_factory(settings, self.receiver)
        try:
            await asyncio.to_thread(self.socket.connect)
        except Exception:
            await self.close()
            raise RuntimeError('Slack Socket Mode connection failed; check app configuration and connectivity') from None
        self.app.state.delivery.trusted_bot_id = bot_id
        self.connected, self.bridge = True, bridge
        self.app.state.socket, self.app.state.federation_bridge = self.socket, bridge
        return bridge

    async def close(self):
        if self.socket is not None:
            await asyncio.to_thread(self.socket.close)
            self.socket = None
        if self.receiver is not None:
            self.receiver._commands.shutdown(wait=False, cancel_futures=True)
            self.receiver = None
        self.connected = False


@asynccontextmanager
async def delivery_lifespan(app, settings, http_client, socket_factory):
    from src.agentbus_service import MessageStore, SlackPoster

    store = MessageStore(settings.db_path)
    client = http_client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    poster = SlackPoster(settings, client)
    transport = DeliveryTransport(app, store, settings, client, poster, socket_factory)
    task = None
    app.state.store, app.state.poster = store, poster
    app.state.socket, app.state.federation_bridge = None, None
    app.state.delivery = LocalDeliveryWorker(store, poster, settings.slack_channel)
    try:
        try:
            await transport.connect()
        except RuntimeError:
            if settings.local_delivery_version != 1:
                raise
            LOGGER.warning('Slack bootstrap unavailable; explicit local custody mode retained')
        task = asyncio.create_task(app.state.delivery.run(transport.bridge, connect=transport.connect))
        yield
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await transport.close()
        if http_client is None:
            await client.aclose()
        store.close()
