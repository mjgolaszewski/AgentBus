"""Single transport task; explicit offline bootstrap never trusts an unknown bot."""
from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager, suppress

import httpx

from src.agentbus_delivery_runtime_service import DeliveryCoordinator
from src.agentbus_federation_inbox_service import bind_ordinary_delivery
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
        if bridge is not None:
            if bridge.channel not in settings.channels:
                raise ValueError("Federation channel is not configured")
            bind_ordinary_delivery(bridge, self.store, bridge.channel)
        from src.agentbus_status_api_service import operator_dashboard
        self.receiver = SlackReceiver(self.store, settings.channels, bot_id, settings, bridge,
                                      dashboard=lambda: operator_dashboard(self.app))
        self.socket = self.socket_factory(settings, self.receiver)
        try:
            await asyncio.to_thread(self.socket.connect)
        except Exception:
            await self.close()
            raise RuntimeError('Slack Socket Mode connection failed; check app configuration and connectivity') from None
        self.app.state.delivery.bind_transport(bot_id, bridge)
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
    from src.agentbus_channel_migration_service import check_startup, database_lease
    from src.agentbus_service import MessageStore, SlackPoster

    lease = database_lease(settings.db_path)
    lease.__enter__()
    try:
        check_startup(settings.db_path)
        store = MessageStore(settings.db_path)
        store.local_delivery.configure(settings.local_delivery_outstanding_limit,
                                       settings.local_delivery_retained_limit,
                                       settings.local_delivery_observation_limit)
        store.channels.configure(settings.channels)
    except BaseException:
        lease.__exit__(None, None, None)
        raise
    client = http_client or httpx.AsyncClient(timeout=15, follow_redirects=False)
    posters = {channel: SlackPoster(settings, client, channel=channel, store=store) for channel in settings.channels}
    poster = next(iter(posters.values()))
    transport = DeliveryTransport(app, store, settings, client, poster, socket_factory)
    task = None
    app.state.runtime_started = time.monotonic()
    app.state.store, app.state.poster = store, poster
    app.state.posters = posters
    app.state.socket, app.state.federation_bridge = None, None
    app.state.delivery = DeliveryCoordinator(store, posters)
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
        lease.__exit__(None, None, None)
