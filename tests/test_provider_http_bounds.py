"""Provider HTTP admission bounds apply before JSON or trusted success handling."""
import asyncio
import gzip

import httpx
import pytest


@pytest.mark.parametrize('streamed', [False, True])
def test_shared_provider_response_rejects_oversize_or_compression_before_success(streamed):
    from src.agentbus_federation_slack_service import bounded_response

    async def scenario():
        class Raw(httpx.AsyncByteStream):
            def __init__(self, content):
                self.content = content
            async def __aiter__(self):
                yield self.content[:16]
                yield self.content[16:]
        for headers, content in [({}, b'x' * 33), ({'Content-Encoding': 'gzip'}, gzip.compress(b'{}'))]:
            async def respond(request):
                return (httpx.Response(200, headers=headers, stream=Raw(content)) if headers or streamed else
                        httpx.Response(200, content=content))
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                with pytest.raises(ValueError):
                    await bounded_response(client, 'test-token', 'auth.test', max_bytes=32)
    asyncio.run(scenario())


def test_shared_provider_response_cancels_slow_stream_at_absolute_deadline():
    from src.agentbus_federation_slack_service import bounded_response

    class Slow(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.005)
                yield b' '
        async def aclose(self):
            self.closed = True

    async def scenario():
        stream = Slow()
        async def respond(request):
            return httpx.Response(200, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(TimeoutError):
                await bounded_response(client, 'test-token', 'auth.test', timeout_seconds=0.02)
        assert stream.closed
    asyncio.run(scenario())


def test_shared_provider_response_preserves_status_headers_and_exact_json():
    from src.agentbus_federation_slack_service import bounded_response

    async def scenario():
        def respond(request):
            assert request.headers['Accept-Encoding'] == 'identity'
            assert request.headers['Authorization'] == 'Bearer test-token'
            return httpx.Response(429, headers={'Retry-After': '120'}, json={'ok': False})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            response = await bounded_response(client, 'test-token', 'auth.test')
            assert response.status_code == 429
            assert response.headers['Retry-After'] == '120'
            assert response.json() == {'ok': False}
    asyncio.run(scenario())


@pytest.mark.parametrize('component', ['identity', 'ordinary', 'federation'])
def test_existing_provider_consumers_fail_closed_on_response_budget(monkeypatch, tmp_path, component):
    from fastapi import HTTPException

    from src import agentbus_federation_slack_service as adapter
    from src.agentbus_federation_bridge_service import ProviderUnavailable
    from src.agentbus_service import SendMessage, Settings, SlackPoster

    monkeypatch.setattr(adapter, 'MAX_PROVIDER_RESPONSE_BYTES', 64)
    configured = Settings(api_token='local', slack_bot_token='xoxb-test', slack_app_token='xapp-test',
                          slack_channel='C123456', db_path=tmp_path / 'unused.sqlite3')
    async def scenario():
        def respond(request):
            return httpx.Response(200, json={'ok': True, 'bot_id': 'B123', 'channel': 'C123456',
                'ts': '100.000001', 'message': {'text': 'signed'}, 'padding': 'x' * 128})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            poster = SlackPoster(configured, client)
            if component == 'identity':
                with pytest.raises(RuntimeError, match='identity verification failed'):
                    await poster.authenticated_bot_id()
            elif component == 'ordinary':
                with pytest.raises(HTTPException) as failure:
                    await poster.post(SendMessage(sender='agentbus:flower', text='ordinary'))
                assert failure.value.status_code == 502
            else:
                with pytest.raises(ProviderUnavailable):
                    await adapter.SlackFederationProvider(client, 'xoxb-test', 'C123456').publish('signed')
    asyncio.run(scenario())


@pytest.mark.parametrize('excess', [0, 1])
def test_default_provider_stream_enforces_declared_eight_mib_boundary(excess):
    from src.agentbus_federation_slack_service import bounded_response

    class Sized(httpx.AsyncByteStream):
        closed = False
        async def __aiter__(self):
            chunk = b'x' * 65536
            for _ in range(128):
                yield chunk
            if excess:
                yield b'x'
        async def aclose(self):
            self.closed = True
    async def scenario():
        stream = Sized()
        def respond(request):
            return httpx.Response(200, stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            if excess:
                with pytest.raises(ValueError):
                    await bounded_response(client, 'test-token', 'auth.test')
            else:
                response = await bounded_response(client, 'test-token', 'auth.test')
                assert len(response.content) == 8_388_608
        assert stream.closed
    asyncio.run(scenario())


def test_absolute_provider_deadline_preserves_ordinary_gateway_timeout(monkeypatch, tmp_path):
    from functools import partial

    from fastapi import HTTPException

    from src import agentbus_service as service
    from src.agentbus_federation_slack_service import bounded_response

    class Slow(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(0.005)
                yield b' '
    async def scenario():
        def respond(request):
            return httpx.Response(200, stream=Slow())
        monkeypatch.setattr(service, 'bounded_response', partial(bounded_response, timeout_seconds=0.02))
        configured = service.Settings(api_token='local', slack_bot_token='xoxb-test', slack_app_token='xapp-test',
                                      slack_channel='C123456', db_path=tmp_path / 'unused.sqlite3')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(HTTPException) as failure:
                await service.SlackPoster(configured, client).post(service.SendMessage(sender='agentbus:flower', text='local'))
            assert failure.value.status_code == 504
    asyncio.run(scenario())


def test_provider_history_rejects_contradictory_message_channel():
    from src.agentbus_federation_bridge_service import ProviderUnavailable
    from src.agentbus_federation_slack_service import SlackFederationProvider

    async def scenario():
        def respond(request):
            return httpx.Response(200, json={'ok': True, 'has_more': False, 'messages': [
                {'channel': 'COTHER', 'app_id': 'A123', 'bot_id': 'B123',
                 'ts': '100.000001', 'text': 'observed'}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(ProviderUnavailable):
                await SlackFederationProvider(client, 'test-token', 'C123456').history('0', 100)
    asyncio.run(scenario())
