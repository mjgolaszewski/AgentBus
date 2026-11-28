"""Capacity notices are optional bounded observations, never automatic recovery."""
import asyncio
from dataclasses import replace

import httpx

from src.agentbus_capacity_notice_service import CapacityNoticeWorker
from tests.test_slack_commands import enrolled as enrolled


def test_notice_thresholds_restart_dedup_recovery_and_budget(enrolled, monkeypatch):
    settings, store, _, _ = enrolled
    settings = replace(settings, federation_capacity_notices=True)
    count = [80]
    monkeypatch.setattr(store.federation, 'capacity', lambda: {
        'per_table_limit': 100, 'rows': {'bridge_obligations': count[0]}})
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={'ok': True})

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            worker = CapacityNoticeWorker(store, settings, client)
            worker.bind_channel(settings.slack_channel)
            worker.sample(now=100)
            assert worker.pending()
            assert await worker.submit_once(budget=0) == 0
            assert await worker.submit_once() == 1
            worker = CapacityNoticeWorker(store, settings, client)
            worker.bind_channel(settings.slack_channel)
            worker.sample(now=200)
            assert not worker.pending()  # Durable deduplication survives restart.
            count[0] = 100
            worker.sample(now=300)
            assert await worker.submit_once() == 1
            count[0] = 75
            worker.sample(now=400)
            assert not worker.pending()  # Hysteresis, not threshold chatter.
            count[0] = 60
            worker.sample(now=500)
            assert await worker.submit_once() == 1
            worker.sample(now=600)
            assert not worker.pending()
    asyncio.run(exercise())
    assert len(calls) == 3
    assert all('chat.postEphemeral' in str(r.url) for r in calls)
    assert store.federation.custody_row_limit == 1024


def test_uncertain_notice_is_not_retried(enrolled, monkeypatch):
    settings, store, _, _ = enrolled
    settings = replace(settings, federation_capacity_notices=True)
    monkeypatch.setattr(store.federation, 'capacity', lambda: {
        'per_table_limit': 100, 'rows': {'bridge_obligations': 95}})
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout('lost response')

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            worker = CapacityNoticeWorker(store, settings, client)
            worker.bind_channel(settings.slack_channel)
            worker.sample(now=100)
            assert await worker.submit_once() == 1
            worker.sample(now=200)
            assert await worker.submit_once() == 0
    asyncio.run(exercise())
    assert len(calls) == 1
    assert store._db.execute('SELECT state FROM federation_capacity_notices').fetchone()[0] == 'uncertain'


def test_revoked_notice_recipient_gets_no_provider_call(enrolled, monkeypatch):
    settings, store, _, _ = enrolled
    settings = replace(settings, federation_capacity_notices=True)
    monkeypatch.setattr(store.federation, 'capacity', lambda: {
        'per_table_limit': 100, 'rows': {'bridge_obligations': 100}})

    def forbidden(_request):
        raise AssertionError('revoked recipient caused provider call')

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            worker = CapacityNoticeWorker(store, settings, client)
            worker.bind_channel(settings.slack_channel)
            worker.sample(now=100)
            assert worker.pending()
            worker.settings = replace(settings, federation_capacity_notices=False, slack_operator_user_ids=frozenset())
            assert not worker.pending()
            assert await worker.submit_once() == 0
    asyncio.run(exercise())
