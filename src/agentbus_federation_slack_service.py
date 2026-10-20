"""Outbound Slack federation submission and bounded provider-history recovery."""

from __future__ import annotations

import asyncio
import json
import re
from decimal import Decimal, InvalidOperation

import httpx

from src.agentbus_federation_bridge_service import ProviderCarrierRejected, ProviderUnavailable
from src.agentbus_presentation_service import section_blocks, visible_text

MAX_PROVIDER_RESPONSE_BYTES = 8_388_608
HISTORY_SCAN_SECONDS = 15


class InvalidHistoryPage(ProviderUnavailable):
    """Untrusted provider history cannot establish recovery coverage."""


def history_events(messages: list, channel: str, after: str) -> list[dict]:
    events = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get('ts'), str):
            raise InvalidHistoryPage()
        if message.get('channel', channel) != channel:
            raise InvalidHistoryPage()
        try:
            if not Decimal(message['ts']).is_finite() or Decimal(message['ts']) <= Decimal(after):
                raise InvalidHistoryPage()
        except InvalidOperation as exc:
            raise InvalidHistoryPage() from exc
        events.append({'publisher_id': publisher_id(message, channel),
                       'text': message.get('text', ''), 'ts': message['ts'],
                       **{key: message[key] for key in ('blocks', 'thread_ts', 'subtype', 'hidden')
                          if key in message}})
    return events


async def bounded_response(client: httpx.AsyncClient, token: str, method: str,
                           body: dict | None = None, *, max_bytes: int | None = None,
                           timeout_seconds: float = 15) -> httpx.Response:
    """Bound raw provider input before JSON parsing and before trusting success."""
    limit = MAX_PROVIDER_RESPONSE_BYTES if max_bytes is None else max_bytes
    async with asyncio.timeout(timeout_seconds):
        async with client.stream('POST', 'https://slack.com/api/' + method, json=body,
                headers={'Authorization': 'Bearer ' + token, 'Accept-Encoding': 'identity'}) as response:
            if response.headers.get('Content-Encoding', '').lower() not in {'', 'identity'}:
                raise ValueError('unsupported provider response encoding')
            if response.is_stream_consumed:
                # Mock transports may supply an already-buffered response.
                raw = response.content
                if len(raw) > limit:
                    raise ValueError('provider response exceeds byte limit')
            else:
                received = bytearray()
                async for chunk in response.aiter_raw():
                    if len(received) + len(chunk) > limit:
                        raise ValueError('provider response exceeds byte limit')
                    received.extend(chunk)
                raw = bytes(received)
            return httpx.Response(response.status_code, headers=response.headers, content=raw)


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
        self._scan: dict | None = None

    async def _request(self, method: str, body: dict) -> dict:
        try:
            response = await bounded_response(self._client, self._token, method, body)
            if response.status_code == 429:
                retry = response.headers.get('Retry-After', '60')
                raise ProviderUnavailable(min(86400, max(1, int(retry))) if retry.isdigit() else 60)
            data = response.json()
            if not response.is_success or not isinstance(data, dict) or data.get('ok') is not True:
                raise ProviderUnavailable()
            return data
        except (httpx.HTTPError, ValueError, TimeoutError, RecursionError) as exc:
            raise ProviderUnavailable() from exc

    async def publish(self, text: str) -> str:
        return await self._publish(text, None)

    async def publish_in_thread(self, text: str, thread: str) -> str:
        if not isinstance(thread, str) or not re.fullmatch(r'[1-9][0-9]{0,15}\.[0-9]{1,9}', thread):
            raise ValueError('threaded carrier requires an actual parent reference')
        return await self._publish(text, thread)

    async def _publish(self, text: str, thread: str | None) -> str:
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
        blocks = section_blocks(summary)
        body = {'channel': self.channel, 'text': text, 'blocks': blocks,
            'parse': 'none', 'mrkdwn': False, 'unfurl_links': False, 'unfurl_media': False}
        if thread is not None:
            body.update(thread_ts=thread, reply_broadcast=True)
        data = await self._request('chat.postMessage', body)
        if data.get('channel') != self.channel or not isinstance(data.get('ts'), str):
            raise ProviderUnavailable()
        validate_carrier_echo(data, text, data['ts'])
        if thread is not None and data['message'].get('thread_ts') != thread:
            raise ProviderCarrierRejected('changed_echo', data['ts'])
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
            events.extend(history_events(messages, self.channel, after))
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

    async def scan_history(self, after: str, limit: int) -> tuple[list[dict], bool]:
        """Return an oldest prefix only after complete, resumable reverse coverage.

        Time-page continuation and candidates are disposable, never custody.
        A restart rescans from the durable checkpoint. Ordinary local uncertain
        post recovery deliberately keeps using the complete-history port above.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('invalid history scan budget')
        if self._scan is None or self._scan['subject'] != (after, limit):
            self._scan = {'subject': (after, limit), 'latest': None,
                          'events': [], 'truncated': False}
        scan = self._scan
        page_limit = min(100, limit)
        try:
            async with asyncio.timeout(HISTORY_SCAN_SECONDS):
                for _ in range(10):
                    body = {'channel': self.channel, 'oldest': after,
                            'limit': page_limit, 'inclusive': False}
                    if scan['latest'] is not None:
                        body['latest'] = scan['latest']
                    data = await self._request('conversations.history', body)
                    messages = data.get('messages')
                    metadata = data.get('response_metadata', {})
                    if not isinstance(messages, list) or len(messages) > page_limit or not isinstance(metadata, dict):
                        raise ValueError('invalid history page')
                    events = history_events(messages, self.channel, after)
                    previous = Decimal(scan['latest']) if scan['latest'] is not None else None
                    for event in events:
                        stamp = Decimal(event['ts'])
                        if previous is not None and stamp >= previous:
                            raise ValueError('history pages are not strictly newest first')
                        previous = stamp
                    cursor = metadata.get('next_cursor', '')
                    more = bool(data.get('has_more') or cursor)
                    if not isinstance(cursor, str) or more and not events:
                        raise ValueError('history continuation made no progress')
                    candidates = sorted(scan['events'] + events, key=lambda event: Decimal(event['ts']))
                    truncated = scan['truncated'] or len(candidates) > limit
                    candidates = candidates[:limit]
                    sizes = [len(json.dumps(event, ensure_ascii=True).encode()) for event in candidates]
                    retained_bytes = sum(sizes)
                    while retained_bytes > MAX_PROVIDER_RESPONSE_BYTES and len(candidates) > 1:
                        candidates.pop()
                        retained_bytes -= sizes.pop()
                        truncated = True
                    if retained_bytes > MAX_PROVIDER_RESPONSE_BYTES:
                        raise ValueError('history event exceeds retained scan byte budget')
                    scan.update(events=candidates, latest=events[-1]['ts'] if events else scan['latest'],
                                truncated=truncated)
                    if not more:
                        self._scan = None
                        return candidates, not truncated
        except (ValueError, InvalidOperation, RecursionError, InvalidHistoryPage) as exc:
            self._scan = None
            raise ProviderUnavailable() from exc
        except TimeoutError as exc:
            raise ProviderUnavailable() from exc
        return [], False
