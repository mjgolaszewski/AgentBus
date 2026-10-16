"""Outbound Slack federation submission and bounded provider-history recovery."""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

import httpx

from src.agentbus_federation_bridge_service import ProviderCarrierRejected, ProviderUnavailable
from src.agentbus_presentation_service import visible_text


def publisher_id(event: dict, channel: str) -> str:
    return '/'.join(str(event.get(key, '')) for key in ('app_id', 'bot_id')) + '/' + channel


def validate_carrier_echo(data: dict, text: str, reference: str, *, locator: str | None = None) -> None:
    """One guard for exact provider carrier custody, with an optional local locator."""
    metadata = data.get('response_metadata', {})
    warnings = metadata.get('warnings', []) if isinstance(metadata, dict) else []
    if any(isinstance(items, list) and 'message_truncated' in items
           for items in (warnings, data.get('warnings', []))) or data.get('warning') == 'message_truncated':
        raise ProviderCarrierRejected('truncation_warning', reference)
    echoed = data.get('message')
    if not isinstance(echoed, dict) or not isinstance(echoed.get('text'), str):
        raise ProviderCarrierRejected('missing_echo', reference)
    if echoed['text'] != text:
        raise ProviderCarrierRejected('changed_echo', reference)
    if locator is not None:
        blocks = echoed.get('blocks')
        if (not isinstance(blocks, list) or
                sum(isinstance(b, dict) and b.get('type') == 'context' and b.get('block_id') == locator
                    for b in blocks) != 1):
            raise ProviderCarrierRejected('missing_echo', reference)


class SlackFederationProvider:
    def __init__(self, client: httpx.AsyncClient, token: str, channel: str):
        self._client, self._token, self.channel = client, token, channel

    async def _request(self, method: str, body: dict) -> dict:
        try:
            response = await self._client.post('https://slack.com/api/' + method, json=body,
                headers={'Authorization': 'Bearer ' + self._token})
            if response.status_code == 429:
                retry = response.headers.get('Retry-After', '60')
                raise ProviderUnavailable(min(86400, max(1, int(retry))) if retry.isdigit() else 60)
            data = response.json()
            if not response.is_success or not isinstance(data, dict) or data.get('ok') is not True:
                raise ProviderUnavailable()
            return data
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable() from exc

    async def publish(self, text: str) -> str:
        summary = 'AgentBus federation transport'
        try:
            frame = json.loads(text)
        except ValueError:
            frame = None
        if isinstance(frame, dict):
            kind = visible_text(str(frame.get('kind', 'frame')))[:32]
            origin = visible_text(str(frame.get('origin_instance_id', 'unknown')))[:80]
            destination = visible_text(str(frame.get('destination_instance_id', 'unknown')))[:80]
            message = visible_text(str(frame.get('origin_message_id', 'unknown')))[:128]
            summary = f'AgentBus {kind}: {origin} → {destination}\nID {message}'
        blocks = [{'type': 'section', 'text': {'type': 'plain_text', 'text': summary, 'emoji': False}}]
        data = await self._request('chat.postMessage', {'channel': self.channel, 'text': text, 'blocks': blocks,
            'parse': 'none', 'mrkdwn': False, 'unfurl_links': False, 'unfurl_media': False})
        if data.get('channel') != self.channel or not isinstance(data.get('ts'), str):
            raise ProviderUnavailable()
        validate_carrier_echo(data, text, data['ts'])
        return data['ts']

    async def history(self, after: str, limit: int) -> list[dict]:
        events: list[dict] = []
        cursor = ''
        for _ in range(10):
            body = {'channel': self.channel, 'oldest': after, 'limit': min(100, limit), 'inclusive': False}
            if cursor:
                body['cursor'] = cursor
            data = await self._request('conversations.history', body)
            messages = data.get('messages')
            if not isinstance(messages, list) or len(events) + len(messages) > limit:
                raise ProviderUnavailable()
            for message in messages:
                if not isinstance(message, dict) or not isinstance(message.get('ts'), str):
                    raise ProviderUnavailable()
                try:
                    if not Decimal(message['ts']).is_finite() or Decimal(message['ts']) <= Decimal(after):
                        raise ProviderUnavailable()
                except InvalidOperation as exc:
                    raise ProviderUnavailable() from exc
                events.append({'publisher_id': publisher_id(message, self.channel),
                               'text': message.get('text', ''), 'ts': message['ts']})
            metadata = data.get('response_metadata', {})
            if not isinstance(metadata, dict):
                raise ProviderUnavailable()
            next_cursor = metadata.get('next_cursor', '')
            if not data.get('has_more') and not next_cursor:
                return sorted(events, key=lambda event: Decimal(event['ts']))
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                raise ProviderUnavailable()
            cursor = next_cursor
        raise ProviderUnavailable()
