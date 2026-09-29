import asyncio
import json
import sqlite3

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
    assert client.get("/healthz").json() == {"status": "ok", "slack_connected": True}
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
    assert set(API_OPERATIONS) == {"health", "send", "read", "info", "inbox", "claim"}
    assert routes[("GET", "/healthz")] is API_OPERATIONS["health"]
    assert routes[("POST", "/v1/messages")] is API_OPERATIONS["send"]
    assert routes[("GET", "/v1/messages")] is API_OPERATIONS["read"]
    assert routes[("GET", "/v1/info")] is API_OPERATIONS["info"]
    assert routes[("GET", "/v1/inbox")] is API_OPERATIONS["inbox"]
    assert routes[("POST", "/v1/messages/{cursor}/claim")] is API_OPERATIONS["claim"]


def test_send_roundtrip_and_plain_text_blocks_preserve_mentions_and_unicode(service):
    client, app, requests = service
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
    assert page["messages"] == [response.json()]
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


def test_v1_envelopes_remain_compatible_and_new_human_messages_are_unrouted():
    old = json.dumps({"agentbus": 1, "sender": "old-agent", "recipient": "all",
                      "kind": "message", "text": "legacy"})
    _, message = normalize_event(
        event(old, subtype="bot_message", bot_id=BOT_ID), CHANNEL, BOT_ID
    )
    assert message.audience == "broadcast"
    _, human = normalize_event(event("plain Slack text"), CHANNEL, BOT_ID)
    assert human.audience == "unrouted"
