import asyncio
import json
import sqlite3
import uuid
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from slack_sdk.socket_mode.request import SocketModeRequest

from agentbus_service import (
    API_OPERATIONS,
    MessageStore,
    SendMessage,
    Settings,
    SlackReceiver,
    create_app,
    encode_envelope,
    normalize_event,
)

AUTH = {"Authorization": "Bearer local-test-secret"}
CHANNEL = "C123456"
BOT_ID = "B123"


class FakeSocket:
    def __init__(self, settings, receiver):
        self.receiver = receiver
        self.connected = False
        self.acknowledgements = []

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    def send_socket_mode_response(self, response):
        self.acknowledgements.append(response.envelope_id)

    def emit(self, event, envelope="event-1"):
        self.receiver(self, SocketModeRequest(type="events_api", envelope_id=envelope,
                                             payload={"event": event}))


@pytest.fixture
def settings(tmp_path):
    return Settings(api_token="local-test-secret", slack_bot_token="xoxb-test-secret",
                    slack_app_token="xapp-test-secret", slack_channel=CHANNEL,
                    db_path=tmp_path / "state/messages.sqlite3")


@pytest.fixture
def service(settings):
    requests = []

    def respond(request):
        if request.url.path == "/api/auth.test":
            return httpx.Response(200, json={"ok": True, "bot_id": BOT_ID})
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "channel": CHANNEL, "ts": f"1700000000.{len(requests):06}"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app = create_app(settings, http_client=http, socket_factory=FakeSocket)
    with TestClient(app) as client:
        yield client, app, requests
    asyncio.run(http.aclose())


def event(text="Human reply", ts="1700000001.000001", **changes):
    return {"type": "message", "channel": CHANNEL, "user": "U123", "ts": ts, "text": text, **changes}


def test_auth_health_and_invalid_messages_never_contact_slack(service):
    client, app, requests = service
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/v1/status").status_code == 401
    assert client.get("/v1/status", headers=AUTH).json()["slack_connected"] is True
    for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic local-test-secret"}):
        assert client.get("/v1/messages", headers=headers).status_code == 401
        assert client.post("/v1/messages", headers=headers, json={"sender": "agent-a", "text": "hello"}).status_code == 401
    for changes in ({"channel": "COTHER"}, {"sender": ""}, {"recipient": "not a name"}, {"text": " "},
                    {"text": "x" * 6001}, {"text": "🐍" * 4000}, {"thread_ts": "bad"}):
        body = {"sender": "agent-a", "text": "hello", **changes}
        assert client.post("/v1/messages", headers=AUTH, json=body).status_code == 422
    assert client.get("/v1/messages?after=-1", headers=AUTH).status_code == 422
    assert client.get("/v1/messages?limit=201", headers=AUTH).status_code == 422
    assert requests == []
    assert "secret" not in client.get("/healthz").text


def test_public_api_operation_inventory_owns_registered_routes(settings):
    app = create_app(settings, socket_factory=FakeSocket)
    routes = {
        (method, route.path): route.endpoint
        for route in app.routes
        for method in getattr(route, "methods", set())
    }
    assert set(API_OPERATIONS) == {"health", "status", "send", "read", "info", "inbox", "claim", "claim_recovery", "policy_set", "policy_effective", "session_enroll", "profile_handoff", "rotation_grant", "session_rotate_secret", "session_presence", "session_roster", "session_policy_ack", "session_policy_explain", "session_check_in", "control_stop", "control_issue", "control_status", "control_ack", "session_rename"}
    assert routes[("GET", "/healthz")] is API_OPERATIONS["health"]
    assert routes[("GET", "/v1/status")] is API_OPERATIONS["status"]
    assert routes[("POST", "/v1/messages")] is API_OPERATIONS["send"]
    assert routes[("GET", "/v1/messages")] is API_OPERATIONS["read"]
    assert routes[("GET", "/v1/info")] is API_OPERATIONS["info"]
    assert routes[("GET", "/v1/inbox")] is API_OPERATIONS["inbox"]
    assert routes[("POST", "/v1/messages/{cursor}/claim")] is API_OPERATIONS["claim"]
    assert routes[("POST", "/v1/messages/{cursor}/claim-recovery")] is API_OPERATIONS["claim_recovery"]
    assert routes[("POST", "/v1/policy/revisions")] is API_OPERATIONS["policy_set"]
    assert routes[("GET", "/v1/policy/effective")] is API_OPERATIONS["policy_effective"]
    assert routes[("GET", "/v1/sessions/{session_id}/policy")] is API_OPERATIONS["session_policy_explain"]
    assert routes[("POST", "/v1/sessions")] is API_OPERATIONS["session_enroll"]
    assert routes[("POST", "/v1/sessions/{session_id}/rotation-grants")] is API_OPERATIONS["rotation_grant"]
    assert routes[("POST", "/v1/sessions/{session_id}/rotate-secret")] is API_OPERATIONS["session_rotate_secret"]
    assert routes[("POST", "/v1/sessions/{session_id}/policy-ack")] is API_OPERATIONS["session_policy_ack"]
    assert routes[("POST", "/v1/sessions/{session_id}/check-in")] is API_OPERATIONS["session_check_in"]
    assert routes[("POST", "/v1/controls/stop")] is API_OPERATIONS["control_stop"]
    assert routes[("POST", "/v1/controls")] is API_OPERATIONS["control_issue"]
    assert routes[("GET", "/v1/controls/{control_id}")] is API_OPERATIONS["control_status"]
    assert routes[("POST", "/v1/controls/{control_id}/targets/{session_id}/ack")] is API_OPERATIONS["control_ack"]
    assert routes[("POST", "/v1/sessions/{session_id}/rename")] is API_OPERATIONS["session_rename"]


def test_request_limit_rejects_before_slack_and_private_database(service, settings):
    client, _app, requests = service
    assert settings.db_path.stat().st_mode & 0o777 == 0o600
    oversized = b"x" * 65537
    assert client.post("/v1/messages", headers=AUTH, content=oversized).status_code == 413
    assert client.post("/v1/sessions", headers=AUTH, json={
        "repo": "agentbus", "route": "agentbus:flower", "display_name": "Flower",
        "session_secret": "x" * 513,
    }).status_code == 422
    assert requests == []


def test_sender_assurance_is_service_derived_and_legacy_remains_readable(service):
    client, app, _requests = service
    store = app.state.store.participation
    store.set_policy(scope="global", scope_key="*", values={
        "initial_interval_seconds": 60, "backoff_factor": 2,
        "max_interval_seconds": 1920, "control_check_max_seconds": 60,
        "jitter_fraction": 0, "overdue_grace_seconds": 120,
        "presentation_budget_bytes": None,
    }, actor="operator")
    secret = "session-secret-with-at-least-32-characters"
    _chat, session, revision = store.enroll(
        repo="agentbus", route="agentbus:flower", display_name="Flower", session_secret=secret,
    )
    store.acknowledge_policy(session, secret, revision)
    body = {"sender": "agentbus:flower", "recipient": "agentbus:peer", "text": "Verified"}
    spoof = client.post("/v1/messages", headers=AUTH, json={**body, "sender_assurance": "session"})
    assert spoof.status_code == 422
    wrong = client.post("/v1/messages", headers={**AUTH, "X-AgentBus-Session-Token": "wrong"}, json=body)
    assert wrong.status_code == 403
    verified = client.post("/v1/messages", headers={**AUTH, "X-AgentBus-Session-Token": secret}, json=body)
    assert verified.status_code == 201
    assert verified.json()["sender_assurance"] == "session"
    legacy = client.post("/v1/messages", headers=AUTH, json={**body, "text": "Old client"})
    assert legacy.status_code == 201
    assert legacy.json()["sender_assurance"] == "legacy"
    assert [item["sender_assurance"] for item in client.get("/v1/messages", headers=AUTH).json()["messages"]] == [
        "session", "legacy",
    ]
    assert client.post("/v1/messages", headers=AUTH,
                       json={**body, "sender": "slack:U123"}).status_code == 422
    assert client.post("/v1/messages", headers=AUTH,
                       json={**body, "sender": "AgentBus:Flower"}).status_code == 422


def test_thread_parent_must_exist_before_slack_post(service):
    client, app, requests = service
    body = {"sender": "agent-a", "recipient": "agent-b", "text": "reply",
            "thread_ts": "1700000999.000001"}
    assert client.post("/v1/messages", headers=AUTH, json=body).status_code == 422
    assert requests == []
    app.state.store.append(CHANNEL, body["thread_ts"],
                           SendMessage(sender="agent-b", recipient="agent-a", text="root"))
    assert client.post("/v1/messages", headers=AUTH, json=body).status_code == 201


def test_slack_display_escapes_invisible_controls_but_history_is_raw(service):
    client, _app, requests = service
    raw = "look\u202eaway\u200b\x1b[31m"
    posted = client.post("/v1/messages", headers=AUTH,
                         json={"sender": "agent-a", "recipient": "agent-b", "text": raw})
    assert posted.status_code == 201
    blocks = json.loads(requests[0].content)["blocks"]
    display = blocks[1]["text"]["text"]
    assert "\u202e" not in display and "\u200b" not in display and "\x1b" not in display
    assert "\\u202e" in display and "\\u200b" in display and "\\u001b" in display
    assert posted.json()["text"] == raw


def test_operator_policy_write_is_separate_from_message_bearer(service):
    client, app, requests = service
    app.state.settings = replace(app.state.settings, operator_token="operator-secret-with-at-least-32-characters")
    body = {"scope": "global", "scope_key": "*", "values": {
        "initial_interval_seconds": 60, "backoff_factor": 2,
        "max_interval_seconds": 1920, "control_check_max_seconds": 60,
        "jitter_fraction": 0, "overdue_grace_seconds": 120,
        "presentation_budget_bytes": None,
    }}
    assert client.post("/v1/policy/revisions", headers=AUTH, json=body).status_code == 401
    assert client.post("/v1/policy/revisions", headers={"Authorization": "Bearer wrong"}, json=body).status_code == 401
    operator = {"Authorization": "Bearer operator-secret-with-at-least-32-characters"}
    result = client.post("/v1/policy/revisions", headers=operator, json=body)
    assert result.status_code == 201
    assert result.json()["revision_id"] > 0
    effective = client.get("/v1/policy/effective?repo=agentbus&chat_id=test", headers=AUTH)
    assert effective.status_code == 200
    assert effective.json()["values"] == body["values"]
    assert effective.json()["sources"]["max_interval_seconds"] == "global"
    assert requests == []


def test_session_join_policy_ack_and_change_require_session_proof(service):
    client, app, requests = service
    app.state.settings = replace(app.state.settings, operator_token="operator-secret-with-at-least-32-characters")
    operator = {"Authorization": "Bearer operator-secret-with-at-least-32-characters"}
    values = {
        "initial_interval_seconds": 60, "backoff_factor": 2,
        "max_interval_seconds": 1920, "control_check_max_seconds": 60,
        "jitter_fraction": 0, "overdue_grace_seconds": 120,
        "presentation_budget_bytes": None,
    }
    assert client.post("/v1/policy/revisions", headers=operator, json={
        "scope": "global", "scope_key": "*", "values": values,
    }).status_code == 201
    secret = "session-secret-with-at-least-32-characters"
    enrollment = client.post("/v1/sessions", headers=AUTH, json={
        "repo": "agentbus", "route": "agentbus:signal-gardener",
        "display_name": "Signal Gardener", "session_secret": secret,
    })
    assert enrollment.status_code == 201
    payload = enrollment.json()
    session_id, revision = payload["session_id"], payload["revision"]
    assert payload["ack_required"] is True
    assert app.state.store.participation.session_state(session_id)["state"] == "joining"
    ack_url = f"/v1/sessions/{session_id}/policy-ack"
    assert client.post(ack_url, headers=AUTH, json={"revision": revision}).status_code == 401
    session_headers = {**AUTH, "X-AgentBus-Session-Token": secret}
    ack = client.post(ack_url, headers=session_headers, json={"revision": revision})
    assert ack.status_code == 200
    assert client.post(ack_url, headers=session_headers, json={"revision": revision}).json() == ack.json()
    rename_url = f"/v1/sessions/{session_id}/rename"
    assert client.post(rename_url, headers=AUTH, json={"route": "agentbus:new-name"}).status_code == 401
    renamed = client.post(rename_url, headers=session_headers, json={"route": "agentbus:new-name"})
    assert renamed.status_code == 200
    assert renamed.json()["chat_id"] == payload["chat_id"]
    assert renamed.json()["old_route"] == "agentbus:signal-gardener"
    check_in = client.post(f"/v1/sessions/{session_id}/check-in", headers=session_headers)
    assert check_in.status_code == 200
    assert check_in.json()["ack_required"] is False
    assert client.post("/v1/policy/revisions", headers=operator, json={
        "scope": "chat", "scope_key": payload["chat_id"], "values": {"max_interval_seconds": 240},
    }).status_code == 201
    changed = client.post(f"/v1/sessions/{session_id}/check-in", headers=session_headers,
                          json={"current_backoff_seconds": 1920})
    assert changed.status_code == 200
    assert changed.json()["ack_required"] is True
    assert changed.json()["revision"] != revision
    assert app.state.store.participation.session_state(session_id)["current_backoff_seconds"] == 1920
    assert client.post(ack_url, headers=session_headers, json={"revision": revision}).status_code == 409
    assert client.post(ack_url, headers=session_headers, json={"revision": changed.json()["revision"]}).status_code == 200
    assert requests == []


def test_existing_profile_handoff_requires_operator_and_preserves_uuid(service):
    client, app, _ = service
    app.state.settings = replace(app.state.settings, operator_token="operator-secret-with-at-least-32-characters")
    operator = {"Authorization": "Bearer operator-secret-with-at-least-32-characters"}
    values = {
        "initial_interval_seconds": 60, "backoff_factor": 2,
        "max_interval_seconds": 1920, "control_check_max_seconds": 60,
        "jitter_fraction": 0, "overdue_grace_seconds": 120,
        "presentation_budget_bytes": None,
    }
    assert client.post("/v1/policy/revisions", headers=operator, json={
        "scope": "global", "scope_key": "*", "values": values,
    }).status_code == 201
    chat_id = str(uuid.uuid4())
    body = {"chat_id": chat_id, "repo": "agentbus", "route": "agentbus:old"}
    assert client.post("/v1/sessions/profile-handoffs", headers=AUTH, json=body).status_code == 401
    handoff = client.post("/v1/sessions/profile-handoffs", headers=operator, json=body)
    assert handoff.status_code == 201
    secret = "session-secret-with-at-least-32-characters"
    enrollment = {"repo": "agentbus", "route": "agentbus:old", "display_name": "Old",
                  "session_secret": secret, "handoff_token": handoff.json()["handoff_token"]}
    joined = client.post("/v1/sessions", headers=AUTH, json=enrollment)
    assert joined.status_code == 201
    assert joined.json()["chat_id"] == chat_id
    presence_url = f"/v1/sessions/{joined.json()['session_id']}/presence"
    assert client.get(presence_url, headers=AUTH).status_code == 401
    presence = client.get(presence_url, headers=operator)
    assert presence.status_code == 200
    assert presence.json()["chat_id"] == chat_id
    assert presence.json()["state"] == "joining"
    session_headers = {**AUTH, "X-AgentBus-Session-Token": secret}
    assert client.post(f"/v1/sessions/{joined.json()['session_id']}/policy-ack",
                       headers=session_headers,
                       json={"revision": joined.json()["revision"]}).status_code == 200
    check_in_url = f"/v1/sessions/{joined.json()['session_id']}/check-in"
    reported = client.post(check_in_url, headers=session_headers,
                           json={"current_backoff_seconds": 120})
    assert reported.status_code == 200
    assert client.get(presence_url, headers=operator).json()["current_backoff_seconds"] == 120
    assert client.post(check_in_url, headers=session_headers,
                       json={"current_backoff_seconds": -1}).status_code == 409
    assert client.post(check_in_url, headers=session_headers,
                       json={"current_backoff_seconds": "NaN"}).status_code == 422
    assert client.post("/v1/sessions", headers=AUTH, json=enrollment).json()["session_id"] == joined.json()["session_id"]
    assert client.post("/v1/sessions", headers=AUTH, json={**enrollment, "session_secret": "other" * 10}).status_code == 409


def test_self_service_join_and_operator_roster_are_separate_authorities(service):
    client, app, _ = service
    app.state.settings = replace(app.state.settings, operator_token="operator-secret-with-at-least-32-characters")
    operator = {"Authorization": "Bearer operator-secret-with-at-least-32-characters"}
    assert client.post("/v1/policy/revisions", headers=operator, json={
        "scope": "global", "scope_key": "*", "values": {
            "initial_interval_seconds": 60, "backoff_factor": 2,
            "max_interval_seconds": 1920, "control_check_max_seconds": 60,
            "jitter_fraction": 0, "overdue_grace_seconds": 120,
            "presentation_budget_bytes": None,
        },
    }).status_code == 201
    body = {"repo": "agentbus", "route": "agentbus:new-chat", "display_name": "New Chat",
            "session_secret": "new-session-secret-with-at-least-32-characters"}
    joined = client.post("/v1/sessions", headers=AUTH, json=body)
    assert joined.status_code == 201
    assert str(uuid.UUID(joined.json()["chat_id"])) == joined.json()["chat_id"]
    assert client.post("/v1/sessions", headers=AUTH, json=body).json() == joined.json()
    assert client.post("/v1/sessions", headers=AUTH, json={**body, "session_secret": "different" * 5}).status_code == 409
    assert client.get("/v1/sessions/roster", headers=AUTH).status_code == 401
    sessions = client.get("/v1/sessions/roster", headers=operator).json()["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["route"] == body["route"]
    assert sessions[0]["chat_id"] == joined.json()["chat_id"]
    assert sessions[0]["state"] == "joining"
    assert "secret" not in json.dumps(sessions)


def test_operator_grant_rotates_only_the_exact_enrolled_session(service):
    client, app, _ = service
    app.state.settings = replace(app.state.settings, operator_token="operator-secret-with-at-least-32-characters")
    operator = {"Authorization": "Bearer operator-secret-with-at-least-32-characters"}
    participation = app.state.store.participation
    participation.set_policy(scope="global", scope_key="*", values={
        "initial_interval_seconds": 60, "backoff_factor": 2,
        "max_interval_seconds": 1920, "control_check_max_seconds": 60,
        "jitter_fraction": 0, "overdue_grace_seconds": 120,
        "presentation_budget_bytes": None,
    }, actor="operator")
    old = "old-session-secret-with-at-least-32-characters"
    new = "new-session-secret-with-at-least-32-characters"
    chat_id, session_id, revision = participation.enroll(
        repo="agentbus", route="agentbus:flower", display_name="Flower", session_secret=old,
    )
    participation.acknowledge_policy(session_id, old, revision)
    grants = f"/v1/sessions/{session_id}/rotation-grants"
    assert client.post(grants, headers=AUTH, json={"chat_id": chat_id}).status_code == 401
    assert client.post(grants, headers=operator, json={"chat_id": str(uuid.uuid4())}).status_code == 409
    issued = client.post(grants, headers=operator, json={"chat_id": chat_id})
    assert issued.status_code == 201
    token = issued.json()["rotation_token"]
    rotate = f"/v1/sessions/{session_id}/rotate-secret"
    body = {"rotation_token": token, "new_session_secret": new}
    assert client.post(rotate, headers=AUTH,
                       json={"rotation_token": "wrong" * 8, "new_session_secret": new}).status_code == 409
    committed = client.post(rotate, headers=AUTH, json=body)
    assert committed.status_code == 200
    assert client.post(rotate, headers=AUTH, json=body).json() == committed.json()
    check_in = f"/v1/sessions/{session_id}/check-in"
    assert client.post(check_in, headers={**AUTH, "X-AgentBus-Session-Token": old}, json={}).status_code == 401
    assert client.post(check_in, headers={**AUTH, "X-AgentBus-Session-Token": new}, json={}).status_code == 200
    assert participation.session_state(session_id)["acknowledged_policy_revision"] == revision


def test_operator_stop_is_snapshotted_prioritized_and_target_acknowledged(service):
    client, app, requests = service
    app.state.settings = replace(app.state.settings, operator_token="operator-secret-with-at-least-32-characters")
    operator = {"Authorization": "Bearer operator-secret-with-at-least-32-characters"}
    values = {
        "initial_interval_seconds": 60, "backoff_factor": 2,
        "max_interval_seconds": 1920, "control_check_max_seconds": 60,
        "jitter_fraction": 0, "overdue_grace_seconds": 120,
        "presentation_budget_bytes": None,
    }
    assert client.post("/v1/policy/revisions", headers=operator, json={
        "scope": "global", "scope_key": "*", "values": values,
    }).status_code == 201
    sessions = []
    for name in ("one", "two"):
        secret = f"session-secret-for-{name}-with-at-least-32-characters"
        enrolled = client.post("/v1/sessions", headers=AUTH, json={
            "repo": "agentbus", "route": f"agentbus:{name}",
            "display_name": name, "session_secret": secret,
        }).json()
        session_id = enrolled["session_id"]
        headers = {**AUTH, "X-AgentBus-Session-Token": secret}
        assert client.post(f"/v1/sessions/{session_id}/policy-ack", headers=headers, json={
            "revision": enrolled["revision"],
        }).status_code == 200
        sessions.append((session_id, headers))
    assert client.post("/v1/controls/stop", headers=AUTH, json={
        "all_current": True, "reason": "end turns",
    }).status_code == 401
    assert client.post("/v1/controls/stop", headers=operator, json={
        "reason": "missing explicit target mode",
    }).status_code == 422
    issued = client.post("/v1/controls/stop", headers=operator, json={
        "all_current": True, "reason": "end turns",
    })
    assert issued.status_code == 201
    control_id = issued.json()["control_id"]
    assert set(issued.json()["target_session_ids"]) == {session for session, _ in sessions}
    first_id, first_headers = sessions[0]
    second_id, second_headers = sessions[1]
    check = client.post(f"/v1/sessions/{first_id}/check-in", headers=first_headers)
    assert check.status_code == 200
    assert check.json()["controls"][0]["control_id"] == control_id
    ack_url = f"/v1/controls/{control_id}/targets/{first_id}/ack"
    assert client.post(ack_url, headers=second_headers).status_code == 401
    ack = client.post(ack_url, headers=first_headers)
    assert ack.status_code == 200 and ack.json()["directive"] == "STOP"
    assert client.post(ack_url, headers=first_headers).json() == ack.json()
    status = client.get(f"/v1/controls/{control_id}", headers=operator).json()["targets"]
    assert {item["session_id"]: item["state"] for item in status} == {
        first_id: "effective", second_id: "issued",
    }
    assert app.state.store.participation.session_state(first_id)["state"] == "stopped"
    assert client.post(f"/v1/sessions/{first_id}/check-in", headers=first_headers).status_code == 409
    assert requests == []


def test_auxiliary_controls_need_operator_and_checkpoint_report_without_stopping(service):
    client, app, _ = service
    app.state.settings = replace(app.state.settings,
                                 operator_token="operator-secret-with-at-least-32-characters")
    operator = {"Authorization": "Bearer operator-secret-with-at-least-32-characters"}
    values = {
        "initial_interval_seconds": 60, "backoff_factor": 2,
        "max_interval_seconds": 1920, "control_check_max_seconds": 60,
        "jitter_fraction": 0, "overdue_grace_seconds": 120,
        "presentation_budget_bytes": None,
    }
    assert client.post("/v1/policy/revisions", headers=operator, json={
        "scope": "global", "scope_key": "*", "values": values,
    }).status_code == 201
    secret = "session-secret-with-at-least-32-characters"
    joined = client.post("/v1/sessions", headers=AUTH, json={
        "repo": "agentbus", "route": "agentbus:one", "display_name": "one",
        "session_secret": secret,
    }).json()
    session_id = joined["session_id"]
    session_headers = {**AUTH, "X-AgentBus-Session-Token": secret}
    assert client.post(f"/v1/sessions/{session_id}/policy-ack", headers=session_headers,
                       json={"revision": joined["revision"]}).status_code == 200
    request = {"kind": "checkpoint_request", "routes": ["agentbus:one"], "reason": "status"}
    assert client.post("/v1/controls", headers=AUTH, json=request).status_code == 401
    issued = client.post("/v1/controls", headers=operator, json=request)
    assert issued.status_code == 201
    control_id = issued.json()["control_id"]
    delivered = client.post(f"/v1/sessions/{session_id}/check-in", headers=session_headers)
    assert delivered.json()["controls"][0]["kind"] == "checkpoint_request"
    ack_url = f"/v1/controls/{control_id}/targets/{session_id}/ack"
    assert client.post(ack_url, headers=session_headers).status_code == 409
    report = {"activity": "reviewing CI", "blockers": [], "waiting_on": [], "work_refs": []}
    acknowledged = client.post(ack_url, headers=session_headers, json={"report": report})
    assert acknowledged.status_code == 200 and acknowledged.json()["directive"] == "CONTINUE"
    assert app.state.store.participation.session_state(session_id)["state"] == "active"
    assert client.post(f"/v1/sessions/{session_id}/check-in", headers=session_headers).status_code == 200


def test_temporary_policy_override_requires_operator_and_exact_session_ack(service):
    client, app, _ = service
    app.state.settings = replace(app.state.settings,
                                 operator_token="operator-secret-with-at-least-32-characters")
    operator = {"Authorization": "Bearer operator-secret-with-at-least-32-characters"}
    values = {
        "initial_interval_seconds": 60, "backoff_factor": 2,
        "max_interval_seconds": 1920, "control_check_max_seconds": 60,
        "jitter_fraction": 0, "overdue_grace_seconds": 120,
        "presentation_budget_bytes": None,
    }
    assert client.post("/v1/policy/revisions", headers=operator, json={
        "scope": "global", "scope_key": "*", "values": values,
    }).status_code == 201
    secret = "session-secret-with-at-least-32-characters"
    joined = client.post("/v1/sessions", headers=AUTH, json={
        "repo": "agentbus", "route": "agentbus:one", "display_name": "one",
        "session_secret": secret,
    }).json()
    session_id = joined["session_id"]
    session_headers = {**AUTH, "X-AgentBus-Session-Token": secret}
    ack_url = f"/v1/sessions/{session_id}/policy-ack"
    assert client.post(ack_url, headers=session_headers,
                       json={"revision": joined["revision"]}).status_code == 200
    request = {"kind": "temporary_policy_override", "routes": ["agentbus:one"],
               "reason": "short focus interval", "values": {"max_interval_seconds": 120},
               "duration_seconds": 60}
    assert client.post("/v1/controls", headers=AUTH, json=request).status_code == 401
    assert client.post("/v1/controls", headers=operator,
                       json={**request, "duration_seconds": 0}).status_code == 422
    issued = client.post("/v1/controls", headers=operator, json=request)
    assert issued.status_code == 201
    control_id = issued.json()["control_id"]
    assert client.get(f"/v1/sessions/{session_id}/policy", headers=AUTH).status_code == 401
    explanation = client.get(f"/v1/sessions/{session_id}/policy", headers=operator).json()
    assert explanation["values"]["max_interval_seconds"] == 120
    assert explanation["sources"]["max_interval_seconds"] == "temporary"
    assert explanation["presence"]["acknowledged_policy_revision"] == joined["revision"]
    delivered = client.post(f"/v1/sessions/{session_id}/check-in", headers=session_headers).json()
    assert delivered["controls"][0]["control_id"] == control_id
    assert client.post(f"/v1/controls/{control_id}/targets/{session_id}/ack",
                       headers=session_headers).json()["directive"] == "CONTINUE"
    updated = client.post(f"/v1/sessions/{session_id}/check-in", headers=session_headers).json()
    assert updated["ack_required"] is True
    assert updated["revision"] == explanation["revision"]
    assert client.post(ack_url, headers=session_headers,
                       json={"revision": joined["revision"]}).status_code == 409
    assert client.post(ack_url, headers=session_headers,
                       json={"revision": updated["revision"]}).status_code == 200


def test_send_roundtrip_and_plain_text_blocks_preserve_mentions_and_unicode(service):
    client, app, requests = service
    app.state.store.append(CHANNEL, "1699999999.000001",
                           SendMessage(sender="agent-a", recipient="agent-b", text="Thread root"))
    text = '<@U123> <!channel> https://example.com/a?b=c&d=e &lt; 🐍 ```code```'
    body = {"sender": "agent-a", "recipient": "agent-b", "text": text, "repo": "org/repo",
            "kind": "question", "correlation_id": "task-1", "thread_ts": "1699999999.000001"}
    response = client.post("/v1/messages", headers=AUTH, json=body)
    assert response.status_code == 201
    assert response.json().items() >= body.items()
    assert response.json()["cursor"] > 0
    request = requests[0]
    payload = json.loads(request.content)
    assert request.url == "https://slack.com/api/chat.postMessage"
    assert request.headers["Authorization"] == "Bearer xoxb-test-secret"
    assert payload["channel"] == CHANNEL
    assert payload["thread_ts"] == body["thread_ts"]
    assert payload["parse"] == "none" and payload["mrkdwn"] is False
    assert payload["unfurl_links"] is False and payload["unfurl_media"] is False
    assert payload["blocks"][1]["text"] == {"type": "plain_text", "text": text, "emoji": False}
    assert "<" not in payload["text"] and "https://" not in payload["text"] and "&" not in payload["text"]
    assert json.loads(payload["text"]) == {
        "agentbus": 2, **body, "audience": "direct", "reply_to_cursor": None,
    }
    app.state.socket.emit(event(payload["text"], ts=response.json()["slack_ts"],
                                subtype="bot_message", bot_id="B123", thread_ts=body["thread_ts"]))
    page = client.get("/v1/messages", headers=AUTH).json()
    assert page["messages"][-1] == response.json()
    assert app.state.socket.acknowledgements == ["event-1"]


def test_long_body_splits_slack_blocks(service):
    client, app, requests = service
    assert client.post("/v1/messages", headers=AUTH, json={"sender": "a", "text": "x" * 6000}).status_code == 201
    blocks = json.loads(requests[0].content)["blocks"]
    assert [len(block["text"]["text"]) for block in blocks[1:]] == [3000, 3000]


def test_cursors_are_independent_durable_and_filter_broadcasts(settings):
    store = MessageStore(settings.db_path)
    first = store.append(CHANNEL, "1.1", SendMessage(sender="a", recipient="b", text="to b"))
    broadcast = store.append(CHANNEL, "2.1", SendMessage(sender="a", text="everyone"))
    third = store.append(CHANNEL, "3.1", SendMessage(sender="b", recipient="c", text="to c"))
    assert store.append(CHANNEL, "2.1", SendMessage(sender="a", text="duplicate")).cursor == broadcast.cursor
    store.append("COTHER", "4.1", SendMessage(sender="x", text="different channel"))
    page = store.read(CHANNEL, recipient="b", limit=1)
    assert page.messages == [first] and page.has_more and page.next_cursor == first.cursor
    next_page = store.read(CHANNEL, after=page.next_cursor, recipient="b", limit=1)
    assert next_page.messages == [broadcast] and not next_page.has_more
    assert next_page.next_cursor == third.cursor
    assert [m.text for m in store.read(CHANNEL, recipient="c").messages] == ["everyone", "to c"]
    store.close()
    restarted = MessageStore(settings.db_path)
    try:
        assert restarted.read(CHANNEL).messages == [first, broadcast, third]
        new = restarted.append(CHANNEL, "5.1", SendMessage(sender="a", recipient="b", text="new"))
        assert restarted.read(CHANNEL, after=next_page.next_cursor, recipient="b").messages == [new]
        assert restarted.read(CHANNEL, after=10000).next_cursor == 10000
    finally:
        restarted.close()


def test_existing_database_migrates_audience_for_sql_routing(settings):
    settings.db_path.parent.mkdir(parents=True)
    database = sqlite3.connect(settings.db_path)
    database.execute("""
        CREATE TABLE messages (
            cursor INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            slack_ts TEXT NOT NULL,
            thread_ts TEXT,
            recipient TEXT NOT NULL,
            received_at TEXT NOT NULL,
            payload TEXT NOT NULL,
            UNIQUE(channel, slack_ts)
        )
    """)
    message = SendMessage(sender="a", recipient="agent:one", text="legacy direct")
    database.execute(
        "INSERT INTO messages(channel, slack_ts, thread_ts, recipient, received_at, payload) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (CHANNEL, "1.1", None, message.recipient, "2026-09-29T00:00:00+00:00",
         message.model_dump_json()),
    )
    database.commit()
    database.close()

    store = MessageStore(settings.db_path)
    try:
        page = store.inbox(CHANNEL, "agent:one")
        assert [item.text for item in page.messages] == ["legacy direct"]
        migrated = store._db.execute(
            "SELECT audience FROM messages WHERE slack_ts = '1.1'"
        ).fetchone()[0]
        assert migrated == "direct"
    finally:
        store.close()


def test_inbox_bounds_sql_results_before_deserializing(settings, monkeypatch):
    store = MessageStore(settings.db_path)
    for index in range(60):
        store.append(
            CHANNEL, f"{index + 1}.1",
            SendMessage(sender="a", recipient="someone:else", text=f"irrelevant {index}"),
        )
    for index in range(3):
        store.append(
            CHANNEL, f"{index + 100}.1",
            SendMessage(sender="a", recipient="agent:one", text=f"relevant {index}"),
        )
    deserialized = 0
    original = store._message

    def counted(row):
        nonlocal deserialized
        deserialized += 1
        return original(row)

    monkeypatch.setattr(store, "_message", counted)
    try:
        page = store.inbox(CHANNEL, "agent:one", limit=2)
        assert [item.text for item in page.messages] == ["relevant 0", "relevant 1"]
        assert page.has_more is True
        assert deserialized == 3
    finally:
        store.close()


def test_thread_inbox_uses_thread_candidates_and_preserves_routes_and_pagination(settings):
    store = MessageStore(settings.db_path)
    root = "1700000000.000001"
    records = [
        (root, "agent:one", "direct", None, "root"),
        ("1700000001.000001", "agent:one", "direct", root, "direct reply"),
        ("1700000002.000001", "agent:two", "direct", root, "foreign reply"),
        ("1700000003.000001", "all", "broadcast", root, "broadcast reply"),
        ("1700000004.000001", "agent:one", "informational", root, "informational reply"),
        ("1700000005.000001", "agent:one", "direct", "1700000009.000001", "other thread"),
    ]
    try:
        for ts, recipient, audience, thread, body in records:
            store.append(CHANNEL, ts, SendMessage(sender="agent:sender", recipient=recipient,
                                                  audience=audience, thread_ts=thread, text=body))
        first = store.inbox(CHANNEL, "agent:one", thread_ts=root, limit=2)
        assert [item.text for item in first.messages] == ["root", "direct reply"]
        assert first.has_more is True
        second = store.inbox(CHANNEL, "agent:one", after=first.next_cursor,
                             thread_ts=root, limit=2)
        assert [item.text for item in second.messages] == ["broadcast reply", "informational reply"]
        assert second.has_more is False
        context = store.inbox(CHANNEL, "agent:one", thread_ts=root, context=True)
        assert [item.text for item in context.messages] == [
            "root", "direct reply", "foreign reply", "broadcast reply", "informational reply",
        ]
        assert context.messages[2].actionable is False
        indexes = {row["name"] for row in store._db.execute("PRAGMA index_list(messages)")}
        assert "messages_thread_cursor" in indexes
    finally:
        store.close()


def test_socket_human_reply_thread_root_and_unknown_channel(service):
    client, app, requests = service
    socket = app.state.socket
    root = "1700000000.000001"
    socket.emit(event("root", ts=root))
    socket.emit(event("reply", thread_ts=root))
    socket.emit(event("reply duplicate", thread_ts=root), envelope="retry")
    socket.emit(event("private", channel="COTHER", ts="1700000002.000001"))
    socket.emit(event("edited", subtype="message_changed", ts="1700000003.000001"))
    socket.emit(event("non-message", type="reaction_added", ts="1700000004.000001"))
    page = client.get("/v1/messages", headers=AUTH, params={"thread_ts": root}).json()
    assert [message["text"] for message in page["messages"]] == ["root", "reply"]
    assert page["messages"][1]["sender"] == "slack:U123"
    assert page["messages"][1]["recipient"] == "all"
    assert page["messages"][1]["thread_ts"] == root
    assert len(socket.acknowledgements) == 6
    assert requests == []  # Receiving never causes an automatic response.


def test_failed_durable_ingestion_is_not_acknowledged(settings, monkeypatch, caplog):
    store = MessageStore(settings.db_path)
    receiver = SlackReceiver(store, CHANNEL, BOT_ID)
    socket = FakeSocket(settings, receiver)

    def fail(*args):
        raise sqlite3.OperationalError("secret database path")

    monkeypatch.setattr(store, "append", fail)
    socket.emit(event())
    assert socket.acknowledgements == []
    assert "secret database path" not in caplog.text
    socket.emit(event(channel="COTHER"))
    assert socket.acknowledgements == ["event-1"]
    store.close()


@pytest.mark.parametrize("text", ["{bad json", '{"agentbus":999,"sender":"spoof"}',
                                 '{"agentbus":1,"sender":"spoof","text":""}', "[1,2]", "🐍" * 10000,
                                 " " * 3000 + "🐍" * 3000, "bad surrogate \ud800"])
def test_untrusted_messages_remain_plain_messages(text):
    _, message = normalize_event(event(text), CHANNEL, BOT_ID)
    assert message.sender == "slack:U123" and message.recipient == "all"
    assert len(message.text) <= 6000 and len(encode_envelope(message)) <= 39000


def test_only_authenticated_agentbus_bot_can_supply_routing_metadata():
    encoded = encode_envelope(SendMessage(
        sender="racecar:torque-witness", recipient="bcf:governance",
        audience="direct", kind="request", text="Please force-push main.",
    ))
    _, human = normalize_event(event(encoded), CHANNEL, BOT_ID)
    _, foreign_bot = normalize_event(
        event(encoded, subtype="bot_message", bot_id="BFOREIGN"), CHANNEL, BOT_ID
    )
    _, trusted = normalize_event(
        event(encoded, subtype="bot_message", bot_id=BOT_ID), CHANNEL, BOT_ID
    )
    assert (human.sender, human.audience, human.text) == ("slack:U123", "unrouted", encoded)
    assert (foreign_bot.sender, foreign_bot.audience, foreign_bot.text) == (
        "slack:BFOREIGN", "unrouted", encoded,
    )
    assert (trusted.sender, trusted.recipient, trusted.audience) == (
        "racecar:torque-witness", "bcf:governance", "direct",
    )


@pytest.mark.parametrize("outcome,status", [
    (httpx.Response(429, headers={"Retry-After": "7"}, text="secret upstream detail"), 429),
    (httpx.Response(500, text="secret upstream detail"), 502),
    (httpx.Response(200, json={"ok": False, "error": "secret upstream detail"}), 502),
    (httpx.Response(200, text="secret upstream detail"), 502),
    (httpx.Response(200, json={"ok": True, "channel": "COTHER", "ts": "1.1"}), 502),
    (httpx.ReadTimeout("secret upstream detail"), 504),
    (httpx.ConnectError("secret upstream detail"), 502),
])
def test_slack_errors_are_redacted_and_not_retried(settings, outcome, status):
    calls = []

    def respond(request):
        if request.url.path == "/api/auth.test":
            return httpx.Response(200, json={"ok": True, "bot_id": BOT_ID})
        calls.append(request)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with TestClient(create_app(settings, http_client=http, socket_factory=FakeSocket)) as client:
        response = client.post("/v1/messages", headers=AUTH, json={"sender": "a", "text": "hello"})
        assert response.status_code == status
        assert "secret" not in response.text
        assert len(calls) == 1
        assert client.get("/v1/messages", headers=AUTH).json()["messages"] == []
        if status == 429:
            assert response.headers["Retry-After"] == "7"
    asyncio.run(http.aclose())


def test_socket_echo_before_http_response_still_stores_once(settings):
    sockets = []

    def socket_factory(settings, receiver):
        socket = FakeSocket(settings, receiver)
        sockets.append(socket)
        return socket

    def respond(request):
        if request.url.path == "/api/auth.test":
            return httpx.Response(200, json={"ok": True, "bot_id": BOT_ID})
        sockets[0].emit(event(json.loads(request.content)["text"], ts="1.1",
                              subtype="bot_message", bot_id=BOT_ID))
        return httpx.Response(200, json={"ok": True, "channel": CHANNEL, "ts": "1.1"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with TestClient(create_app(settings, http_client=http, socket_factory=socket_factory)) as client:
        response = client.post("/v1/messages", headers=AUTH, json={"sender": "a", "text": "hello"})
        assert response.status_code == 201
        assert client.get("/v1/messages", headers=AUTH).json()["messages"] == [response.json()]
    asyncio.run(http.aclose())
    assert sockets[0].connected is False


def test_persistence_failure_after_post_explains_success(service, monkeypatch):
    client, app, requests = service

    def fail(*args):
        raise sqlite3.OperationalError("private error")

    monkeypatch.setattr(app.state.store, "append", fail)
    response = client.post("/v1/messages", headers=AUTH, json={"sender": "a", "text": "hello"})
    assert response.status_code == 503
    assert "posted to Slack" in response.text and "private error" not in response.text
    assert len(requests) == 1


@pytest.mark.parametrize("outcome", [
    httpx.Response(200, json={"ok": False, "error": "secret detail"}),
    httpx.Response(200, json={"ok": True, "bot_id": "invalid"}),
    httpx.ConnectError("secret detail"),
])
def test_startup_fails_closed_when_slack_bot_identity_is_not_authenticated(settings, outcome):
    def respond(_request):
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with pytest.raises(RuntimeError, match="Slack bot identity verification failed") as failure:
        with TestClient(create_app(settings, http_client=http, socket_factory=FakeSocket)):
            pass
    assert "secret" not in str(failure.value)
    asyncio.run(http.aclose())


def test_env_requires_tokens_without_disclosing_values(monkeypatch, tmp_path):
    for name in ("API_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL"):
        monkeypatch.delenv(f"AGENTBUS_{name}", raising=False)
    with pytest.raises(ValueError, match="AGENTBUS_API_TOKEN"):
        create_app()
    monkeypatch.setenv("AGENTBUS_API_TOKEN", "local-secret" * 4)
    monkeypatch.setenv("AGENTBUS_SLACK_BOT_TOKEN", "xoxb-bot-secret")
    monkeypatch.setenv("AGENTBUS_SLACK_APP_TOKEN", "xapp-app-secret")
    monkeypatch.setenv("AGENTBUS_SLACK_CHANNEL", CHANNEL)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("AGENTBUS_DB_PATH", raising=False)
    settings = Settings.from_env()
    assert settings.db_path == tmp_path / "agentbus/messages.sqlite3"
    assert "secret" not in repr(settings)
    monkeypatch.setenv("AGENTBUS_SLACK_CHANNEL", "#name")
    with pytest.raises(ValueError, match="channel ID"):
        Settings.from_env()
    monkeypatch.setenv("AGENTBUS_SLACK_CHANNEL", CHANNEL)
    monkeypatch.setenv("AGENTBUS_API_TOKEN", "too-short")
    with pytest.raises(ValueError, match="at least 32"):
        Settings.from_env()


def test_info_is_authenticated_stable_and_reports_high_water(service):
    client, app, _ = service
    assert client.get("/v1/info").status_code == 401
    first = client.get("/v1/info", headers=AUTH).json()
    assert first["protocol_version"] == 2
    assert first["high_water_cursor"] == 0
    assert "explicit-routing" in first["features"]
    client.post("/v1/messages", headers=AUTH,
                json={"sender": "repo:weed", "recipient": "repo:fern", "text": "hello"})
    second = client.get("/v1/info", headers=AUTH).json()
    assert second["inbox_id"] == first["inbox_id"]
    assert second["channel"] == CHANNEL and second["high_water_cursor"] == 1


def test_actionable_messages_require_explicit_routing(service):
    client, _, requests = service
    ambiguous = client.post("/v1/messages", headers=AUTH,
                            json={"sender": "repo:weed", "kind": "question", "text": "Who owns this?"})
    assert ambiguous.status_code == 422
    broadcast = client.post("/v1/messages", headers=AUTH,
                            json={"sender": "repo:weed", "recipient": "all", "audience": "broadcast",
                                  "kind": "question", "text": "Who owns this?"})
    assert broadcast.status_code == 201
    assert len(requests) == 1


def test_direct_message_is_not_actionable_by_another_agent(service):
    client, _, _ = service
    sent = client.post("/v1/messages", headers=AUTH,
                       json={"sender": "human:owner", "recipient": "tools:weed", "audience": "direct",
                             "kind": "question", "text": "Weed, can you check this?"}).json()
    assert client.get("/v1/inbox", headers=AUTH,
                      params={"identity": "bcf:thistle"}).json()["messages"] == []
    context = client.get("/v1/inbox", headers=AUTH,
                         params={"identity": "bcf:thistle", "context": True}).json()["messages"]
    assert context[0]["cursor"] == sent["cursor"]
    assert context[0]["actionable"] is False
    assert context[0]["action_reason"] == "addressed to tools:weed"
    wrong_reply = client.post("/v1/messages", headers=AUTH,
                              json={"sender": "bcf:thistle", "recipient": "human:owner",
                                    "audience": "direct", "kind": "reply", "text": "I answered",
                                    "reply_to_cursor": sent["cursor"]})
    assert wrong_reply.status_code == 409
    wrong_target = client.post("/v1/messages", headers=AUTH,
                               json={"sender": "tools:weed", "recipient": "someone:else",
                                     "audience": "direct", "kind": "reply", "text": "On it",
                                     "reply_to_cursor": sent["cursor"]})
    assert wrong_target.status_code == 422
    right_reply = client.post("/v1/messages", headers=AUTH,
                              json={"sender": "tools:weed", "recipient": "human:owner",
                                    "audience": "direct", "kind": "reply", "text": "On it",
                                    "reply_to_cursor": sent["cursor"]})
    assert right_reply.status_code == 201


def test_legacy_clients_receive_additive_upgrade_instructions_without_message_breakage(service):
    client, _, _ = service
    sent = client.post("/v1/messages", headers=AUTH, json={
        "sender": "agentbus:old", "recipient": "agentbus:peer", "audience": "direct",
        "kind": "request", "text": "Please upgrade", "reply_to_cursor": None,
    })
    assert sent.status_code == 201
    modern = {**AUTH, "X-AgentBus-Client-Capabilities": "participation-v1"}
    assert "upgrade_notice" not in client.get("/v1/inbox", headers=modern,
                                               params={"identity": "agentbus:old"}).json()
    old_inbox = client.get("/v1/inbox", headers=AUTH, params={"identity": "agentbus:old"}).json()
    assert "agentbus join" in old_inbox["upgrade_notice"]
    assert "acknowledged stop controls" in old_inbox["upgrade_notice"]
    assert "upgrade_notice" in client.get("/v1/info", headers=AUTH).json()
    assert "upgrade_notice" not in client.get("/v1/inbox", headers=AUTH,
                                               params={"identity": "agentbus:old"}).json()
    assert "upgrade_notice" not in client.get("/healthz").json()
    legacy_read = client.get("/v1/messages", headers=AUTH).json()
    modern_read = client.get("/v1/messages", headers=modern).json()
    assert legacy_read["messages"] == modern_read["messages"]
    assert legacy_read["messages"][0]["text"] == "Please upgrade"
    assert "upgrade_notice" not in legacy_read
    assert "upgrade_notice" not in client.get("/v1/inbox", headers=modern,
                                               params={"identity": "agentbus:old"}).json()
    assert "upgrade_notice" not in client.get("/v1/info", headers=modern).json()


def test_legacy_upgrade_notice_is_durable_across_service_restart(settings):
    first = MessageStore(settings.db_path)
    assert first.first_legacy_notice(settings.slack_channel, "agentbus:old") is True
    writes = first._db.total_changes
    assert first.first_legacy_notice(settings.slack_channel, "agentbus:old") is False
    assert first._db.total_changes == writes
    first.close()
    restarted = MessageStore(settings.db_path)
    assert restarted.first_legacy_notice(settings.slack_channel, "agentbus:old") is False
    assert restarted.first_legacy_notice(settings.slack_channel, "agentbus:other") is True
    restarted.close()


def test_unrouted_slack_message_requires_atomic_claim_before_reply(service):
    client, app, _ = service
    app.state.socket.emit(event("Could someone inspect this?"))
    message = client.get("/v1/messages", headers=AUTH).json()["messages"][0]
    assert message["audience"] == "unrouted"
    assert client.get("/v1/inbox", headers=AUTH,
                      params={"identity": "tools:weed"}).json()["messages"] == []
    claim = client.post(f"/v1/messages/{message['cursor']}/claim", headers=AUTH,
                        json={"identity": "tools:weed"})
    assert claim.status_code == 200 and claim.json()["identity"] == "tools:weed"
    competing = client.post(f"/v1/messages/{message['cursor']}/claim", headers=AUTH,
                            json={"identity": "bcf:thistle"})
    assert competing.status_code == 409
    inbox = client.get("/v1/inbox", headers=AUTH,
                       params={"identity": "tools:weed"}).json()["messages"]
    assert inbox[0]["actionable"] is True and inbox[0]["action_reason"] == "claimed by this identity"


def test_claim_recovery_requires_operator_and_changes_exact_current_claim(service):
    client, app, _ = service
    app.state.settings = replace(app.state.settings,
                                 operator_token="operator-secret-with-at-least-32-characters")
    operator = {"Authorization": "Bearer operator-secret-with-at-least-32-characters"}
    app.state.socket.emit(event("Could someone inspect this?"))
    cursor = client.get("/v1/messages", headers=AUTH).json()["messages"][0]["cursor"]
    assert client.post(f"/v1/messages/{cursor}/claim", headers=AUTH,
                       json={"identity": "agentbus:old"}).status_code == 200
    url = f"/v1/messages/{cursor}/claim-recovery"
    payload = {"new_identity": "agentbus:new", "reason": "old chat ended"}
    assert client.post(url, headers=AUTH, json=payload).status_code == 401
    changed = client.post(url, headers=operator, json=payload)
    assert changed.status_code == 200
    assert changed.json()["previous_identity"] == "agentbus:old"
    assert app.state.store.claimant(CHANNEL, cursor) == "agentbus:new"
    assert client.post(url, headers=operator,
                       json={**payload, "new_identity": "bad identity"}).status_code == 422


def test_inbox_paginates_mixed_route_classes_without_skipping_claimed_work(service):
    _, app, _ = service
    store = app.state.store
    expected = []
    for number in range(120):
        audience = ("direct", "informational", "broadcast", "unrouted")[number % 4]
        recipient = "agentbus:flower" if audience in {"direct", "informational"} else "all"
        sender = "slack:human" if audience == "unrouted" else "agentbus:peer"
        message = store.append(CHANNEL, f"1800000000.{number:06d}", SendMessage(
            sender=sender, recipient=recipient, text=f"item {number}", audience=audience,
        ))
        if audience == "unrouted":
            store.claim(CHANNEL, message.cursor, "agentbus:flower")
        expected.append(message.cursor)
    seen = []
    cursor = 0
    while True:
        page = store.inbox(CHANNEL, "agentbus:flower", after=cursor, limit=7)
        seen.extend(item.cursor for item in page.messages)
        cursor = page.next_cursor
        if not page.has_more:
            break
    assert seen == expected


def test_v1_envelopes_remain_compatible_and_new_human_messages_are_unrouted():
    old = json.dumps({"agentbus": 1, "sender": "old-agent", "recipient": "all",
                      "kind": "message", "text": "legacy"})
    _, message = normalize_event(
        event(old, subtype="bot_message", bot_id=BOT_ID), CHANNEL, BOT_ID
    )
    assert message.audience == "broadcast"
    _, human = normalize_event(event("plain Slack text"), CHANNEL, BOT_ID)
    assert human.audience == "unrouted"
