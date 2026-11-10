"""Actual API observations never imply model work or grant notice execution authority."""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from src.agentbus_observation_runtime_service import StaleNoticeWorker
from src.agentbus_observation_service import ObservationStore, load_monitors
from src.agentbus_service import MessageStore, Settings, create_app


@pytest.fixture
def observed(tmp_path):
    clock = [100.0]
    store = MessageStore(tmp_path / "bus.db")
    store.channels.configure(("C111", "C222"))
    store.participation.initialize_default_policy()
    route, secret = "alpha:agentbus:writer", "private-observation-".ljust(40, "x")
    _, session, revision = store.participation.enroll(repo="agentbus", route=route, display_name="writer",
                                                    session_secret=secret, channel="C111")
    store.participation.acknowledge_policy(session, secret, revision)
    principal = store.channels.principal(secret, route)
    monitor = {"session_id": session, "chat_id": principal.chat_id, "channel": "C111",
               "operators": ["U123"], "stale_seconds": 120}
    observation = ObservationStore(store, [monitor], now=lambda: clock[0])
    store.observations = observation
    yield store, observation, principal, secret, monitor, clock
    store.close()


def test_observation_coalescing_and_missing_history_are_honest(observed):
    _, observation, principal, _, _, clock = observed
    assert observation.snapshot_scope(vars(principal))["last_actual_api_access"] is None
    assert observation.next_notice() is None
    observation.observe_scope(vars(principal))
    clock[0] = 110
    observation.observe_scope(vars(principal))
    assert observation.snapshot_scope(vars(principal))["last_actual_api_access"] == 100
    clock[0] = 90
    observation.observe_scope(vars(principal))
    assert observation.snapshot_scope(vars(principal))["last_actual_api_access"] == 100
    clock[0] = 130
    observation.observe_scope(vars(principal))
    assert observation.snapshot_scope(vars(principal))["last_actual_api_access"] == 130
    assert observation.snapshot_scope(vars(principal))["work_observation"] == "not_inferred"
    assert observation.snapshot_scope(vars(principal))["continuous_monitoring"] is False


def test_private_notice_dedup_survives_restart_and_requires_fresh_access(observed):
    store, observation, principal, _, monitor, clock = observed
    observation.observe_scope(vars(principal))
    clock[0] = 219
    assert observation.next_notice() is None
    clock[0] = 220
    notice = observation.next_notice()
    assert observation.reserve_notice(notice)
    assert not observation.reserve_notice(notice)
    restarted = ObservationStore(store, [monitor], now=lambda: clock[0])
    assert restarted.next_notice() is None
    restarted.observe_scope(vars(principal))
    clock[0] = 340
    fresh = restarted.next_notice()
    assert fresh["episode"] != notice["episode"]
    # Another actual access wins a race before provider reservation.
    restarted.observe_scope(vars(principal))
    assert not restarted.reserve_notice(fresh)


def test_api_access_excludes_health_failed_authority_and_operator_dashboard(observed, tmp_path):
    store, observation, principal, secret, _, _ = observed
    settings = Settings(api_token="observation-bearer", slack_bot_token="xoxb-test", slack_app_token="xapp-test",
                        slack_channel="C111", db_path=tmp_path / "bus.db")
    app = create_app(settings)
    app.state.store, app.state.socket = store, None
    headers = {"Authorization": "Bearer observation-bearer", "X-AgentBus-Session-Token": secret,
               "X-AgentBus-Inbox-ID": principal.inbox_id}
    client = TestClient(app)
    try:
        assert client.get("/healthz", headers=headers).status_code == 200
        assert client.get("/v1/status", headers=headers).status_code == 200
        assert observation.snapshot_scope(vars(principal))["last_actual_api_access"] is None
        assert client.get("/v1/info", headers={**headers, "Authorization": "Bearer invalid"}).status_code == 401
        assert observation.snapshot_scope(vars(principal))["last_actual_api_access"] is None
        assert client.get("/v1/info", headers=headers).status_code == 200
        assert observation.snapshot_scope(vars(principal))["last_actual_api_access"] == 100
    finally:
        client.close()


def test_notice_is_private_and_unknown_outcome_does_not_retry(observed):
    _, observation, principal, _, _, clock = observed
    observation.observe_scope(vars(principal))
    clock[0] = 220
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        raise httpx.ReadTimeout("lost response")
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            worker = StaleNoticeWorker(observation, client, SimpleNamespace(slack_bot_token="xoxb-test"))
            assert await worker.submit_once() == 1
            assert await worker.submit_once() == 0
    import asyncio
    asyncio.run(exercise())
    assert calls[0]["channel"] == "C111" and calls[0]["user"] == "U123"
    assert "no wake" in calls[0]["text"] and len(calls) == 1


def test_monitor_configuration_requires_explicit_scoped_allowlisted_operators(observed, tmp_path):
    _, _, _, _, monitor, _ = observed
    path = tmp_path / "monitor.json"
    settings = SimpleNamespace(stale_monitor_config=path, channels=("C111",), slack_operator_user_ids={"U123"})
    path.write_text(json.dumps({"version": 1, "sessions": [monitor]}))
    assert load_monitors(settings) == (monitor,)
    for patch in ({"operators": ["U999"]}, {"channel": "C222"}, {"stale_seconds": True},
                  {"session_id": "not-a-session"}, {"webhook": "https://invalid.example"}):
        path.write_text(json.dumps({"version": 1, "sessions": [{**monitor, **patch}]}))
        with pytest.raises(ValueError, match="private stale-monitor"):
            load_monitors(settings)
    assert load_monitors(SimpleNamespace(stale_monitor_config=None)) == ()


def test_disabled_channel_keeps_exact_control_presence_but_no_ordinary_access(observed, tmp_path):
    store, observation, principal, secret, _, _ = observed
    settings = Settings(api_token="disabled-control-bearer", slack_bot_token="xoxb-test", slack_app_token="xapp-test",
                        slack_channel="C222", db_path=tmp_path / "bus.db")
    app = create_app(settings)
    app.state.store = store
    store.channels.configure(("C222",))
    headers = {"Authorization": "Bearer disabled-control-bearer", "X-AgentBus-Session-Token": secret,
               "X-AgentBus-Inbox-ID": principal.inbox_id}
    client = TestClient(app)
    try:
        response = client.get("/v1/sessions/" + principal.session_id + "/self-presence", headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["channel"] == "C111"
        assert observation.snapshot_scope(vars(principal))["last_actual_api_access"] == 100
        assert client.get("/v1/info", headers=headers).status_code == 403
        assert client.get("/v1/messages", headers=headers).status_code == 403
        assert observation.next_notice() is None
    finally:
        client.close()
