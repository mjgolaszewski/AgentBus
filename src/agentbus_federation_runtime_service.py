"""Opt-in composition inside the existing service and Socket Mode listener."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from src.agentbus_federation_bridge_service import FederationBridge
from src.agentbus_federation_directory_service import DirectorySynchronizer
from src.agentbus_federation_inbox_service import project_message, project_ordinary
from src.agentbus_federation_slack_service import SlackFederationProvider, publisher_id
from src.agentbus_federation_wire_service import Binding, Identity, key_generation


class SlackFederationBridge(FederationBridge):
    channel: str

    def accept_event(self, event: dict) -> bool:
        if (event.get('type') != 'message' or event.get('channel') != self.channel
                or event.get('hidden') or event.get('subtype') not in (None, 'bot_message', 'thread_broadcast')):
            return False
        text = event.get('text')
        if not isinstance(text, str) or 'agentbus_federation' not in text[:200]:
            return False
        try:
            self.receive({'publisher_id': publisher_id(event, self.channel), 'text': text, 'ts': event.get('ts')})
        except (ValueError, KeyError, TypeError):
            pass
        return True


def load_bridge(path: Path, store, client, token: str, bot_id: str) -> SlackFederationBridge:
    if path.stat().st_size > 65_536:
        raise ValueError('federation configuration exceeds limit')
    data = json.loads(path.read_text())
    required = {'instance_id', 'route_prefix', 'publisher_id', 'private_key_file', 'key_generation', 'channel', 'bindings'}
    if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {'directory', 'inbox_projection', 'history_limit'} or not isinstance(data['bindings'], list) or len(data['bindings']) > 64:
        raise ValueError('invalid operator federation configuration')
    local_publisher = data['publisher_id']
    if (not isinstance(local_publisher, str) or len(local_publisher.split('/')) != 3
            or not local_publisher.split('/')[0] or local_publisher.split('/')[1:] != [bot_id, data['channel']]):
        raise ValueError('local federation publisher does not match authenticated bot')
    key_path = Path(data['private_key_file'])
    if not key_path.is_absolute() or key_path.stat().st_mode & 0o077 or key_path.stat().st_size != 32:
        raise ValueError('federation signing key must be a private local 32-byte file')
    private = Ed25519PrivateKey.from_private_bytes(key_path.read_bytes())
    if data['key_generation'] != key_generation(private.public_key()):
        raise ValueError('local signing key differs from the approved generation')
    identity = Identity(data['instance_id'], data['route_prefix'], data['publisher_id'], private)
    bindings = {}
    for item in data['bindings']:
        if (not {'instance_id', 'route_prefix', 'publisher_id', 'public_key', 'key_generation'} <= set(item)
                or set(item) - {'instance_id', 'route_prefix', 'publisher_id', 'public_key', 'key_generation', 'directory_version', 'ordinary_version'}
                or item['instance_id'] in bindings):
            raise ValueError('duplicate or invalid operator origin binding')
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(item['public_key'], validate=True))
        if item['key_generation'] != key_generation(public):
            raise ValueError('peer verification key differs from the approved generation')
        bindings[item['instance_id']] = Binding(item['instance_id'], item['route_prefix'], item['publisher_id'], public,
                                                   directory_version=item.get('directory_version'),
                                                   ordinary_version=item.get('ordinary_version'))
    if any(binding.ordinary_version == 3 for binding in bindings.values()):
        if type(data.get('inbox_projection')) is not int or data['inbox_projection'] != 1:
            raise ValueError('ordinary peers require explicitly enabled inbox projection')
    bridge = SlackFederationBridge(store.federation, identity, bindings,
                                  SlackFederationProvider(client, token, data['channel']),
                                  history_limit=data.get('history_limit', 100))
    bridge.channel = data['channel']
    if 'inbox_projection' in data:
        if type(data['inbox_projection']) is not int or data['inbox_projection'] != 1:
            raise ValueError('unsupported operator inbox projection version')
        bridge.project_message = partial(project_message, store, data['channel'])
        bridge.project_ordinary = partial(project_ordinary, store, data['channel'])
    if 'directory' in data:
        policy = data['directory']
        if (not isinstance(policy, dict) or set(policy) != {'share_chat_ids', 'peers'}
                or not isinstance(policy['share_chat_ids'], list) or not isinstance(policy['peers'], list)):
            raise ValueError('invalid operator directory sharing policy')
        if any(store.channels.channel_for_chat(chat) != data['channel'] for chat in policy['share_chat_ids']):
            raise ValueError('Directory sharing identity belongs to another channel')
        bridge.directory = DirectorySynchronizer(bridge, store.participation,
            share_chat_ids=policy['share_chat_ids'], peers=policy['peers'])
    return bridge


@asynccontextmanager
async def federation_runtime(app, store, settings, client, bot_id: str):
    bridge = (load_bridge(settings.federation_config, store, client, settings.slack_bot_token, bot_id)
              if settings.federation_config is not None else None)
    app.state.federation_bridge = bridge
    task = asyncio.create_task(bridge.run()) if bridge is not None else None
    try:
        yield bridge
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
