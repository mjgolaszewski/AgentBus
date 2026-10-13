"""Cross-service directory and routing propositions using independent stores."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from slack_sdk.socket_mode.request import SocketModeRequest

from src import agentbus_peer_client
from src.agentbus_codex_wake_client import _candidate
from src.agentbus_peer_directory_service import parse_wire, wire
from src.agentbus_send_policy_service import validate_new_send
from src.agentbus_service import MessageStore, SendMessage, Settings, create_app, encode_envelope, normalize_event
from src.agentbus_slack_socket_service import SlackReceiver

CHANNEL = "C123456"


def bot_event(bot: str, text: str, ts: str = "1700000001.000001") -> dict:
    return {"type": "message", "subtype": "bot_message", "channel": CHANNEL,
            "bot_id": bot, "text": text, "ts": ts}


def joined(store: MessageStore, route: str, secret: str) -> None:
    if store.participation._latest_policy("global", "*") is None:
        store.participation.set_policy(scope="global", scope_key="*", values={
            "initial_interval_seconds": 60, "backoff_factor": 2,
            "max_interval_seconds": 1920, "control_check_max_seconds": 60,
            "jitter_fraction": 0, "overdue_grace_seconds": 120,
            "presentation_budget_bytes": None,
        }, actor="operator")
    _chat, session, revision = store.participation.enroll(
        repo=route.split(":")[1], route=route, display_name=route,
        session_secret=secret * 4,
    )
    store.participation.acknowledge_policy(session, secret * 4, revision)


def test_remote_directory_refresh_discovers_silent_joined_chats_without_operator_data(tmp_path):
    home = MessageStore(tmp_path / "home.sqlite3")
    remote = MessageStore(tmp_path / "remote.sqlite3")
    try:
        joined(home, "home:agentbus:signal-gardener", "home-secret")
        joined(home, "home:racecar:torque-witness", "racecar-secret")
        request = bot_event("BREMOTE", wire({"kind": "request", "nonce": str(uuid.uuid4())}))
        parsed = parse_wire(request, CHANNEL, {"moranwm": "BREMOTE"})
        assert parsed is not None and parsed[1] == "moranwm"
        entries, complete = home.peers.local_entries("home")
        assert complete and len(entries) == 2
        reply = bot_event("BHOME", wire({"kind": "snapshot", "nonce": parsed[2]["nonce"],
                                         "entries": entries, "complete": complete}))
        source = parse_wire(reply, CHANNEL, {"home": "BHOME"})
        assert source is not None
        assert remote.peers.apply_snapshot(*source)
        directory = remote.peers.list("moranwm", {"home": "BHOME"})
        assert {agent["route"] for agent in directory["agents"]} == {
            "home:agentbus:signal-gardener", "home:racecar:torque-witness"}
        assert directory["sources"][1]["complete"] is True
        assert "session_id" not in json.dumps(directory)
        assert "secret" not in json.dumps(directory)

        # A fresh empty complete snapshot retracts a departed service's names.
        assert remote.peers.apply_snapshot("BHOME", "home", {"kind": "snapshot", "entries": [],
                                                   "complete": True})
        assert remote.peers.list("moranwm", {"home": "BHOME"})["agents"] == []
    finally:
        home.close()
        remote.close()


def test_only_configured_prefix_bound_bot_can_route_and_wake(tmp_path):
    remote = MessageStore(tmp_path / "remote.sqlite3")
    try:
        joined(remote, "moranwm:agentbus:maintainer", "remote-secret")
        sent = SendMessage(sender="home:agentbus:signal-gardener",
                           recipient="moranwm:agentbus:maintainer",
                           text="Can you inspect the wrapper?", audience="direct")
        sent._sender_assurance = "session"
        body = encode_envelope(sent)
        event = bot_event("BHOME", body)
        ts, accepted = normalize_event(event, CHANNEL, "BREMOTE", {"home": "BHOME"})
        assert accepted._sender_assurance == "peer"
        stored = remote.append(CHANNEL, ts, accepted)
        assert stored.sender_assurance == "peer"
        inbox = remote.inbox(CHANNEL, "moranwm:agentbus:maintainer")
        assert inbox.messages[0].actionable is True
        assert _candidate({"kind": "MESSAGE", "id": str(stored.cursor),
                           "message": inbox.messages[0].model_dump()}, "moranwm:agentbus:maintainer")

        reply = SendMessage(sender="moranwm:agentbus:maintainer", text="I can inspect it",
                            reply_to_cursor=stored.cursor)
        validate_new_send(remote, CHANNEL, reply, "remote-secret" * 4)
        assert reply.recipient == "home:agentbus:signal-gardener"
        assert reply.thread_ts == stored.slack_ts

        for other_bot, sender in (("BUNKNOWN", "home:agentbus:signal-gardener"),
                                  ("BHOME", "moranwm:agentbus:impostor")):
            forged_message = SendMessage(sender=sender,
                recipient="moranwm:agentbus:maintainer", text="forged", audience="direct")
            forged_message._sender_assurance = "session"
            forged = encode_envelope(forged_message)
            _, rejected = normalize_event(bot_event(other_bot, forged), CHANNEL,
                                          "BREMOTE", {"home": "BHOME"})
            assert rejected.audience == "unrouted"
            assert rejected._sender_assurance == "legacy"

        legacy = SendMessage(sender="home:agentbus:signal-gardener",
                             recipient="moranwm:agentbus:maintainer", text="legacy text")
        _, rejected = normalize_event(bot_event("BHOME", encode_envelope(legacy)), CHANNEL,
                                      "BREMOTE", {"home": "BHOME"})
        assert rejected.audience == "unrouted" and rejected._sender_assurance == "legacy"
    finally:
        remote.close()


def test_bad_snapshot_cannot_replace_a_good_one(tmp_path):
    store = MessageStore(tmp_path / "remote.sqlite3")
    try:
        good = {"kind": "snapshot", "entries": [{"route": "home:agentbus:weed",
                                                "display_name": "Weed"}], "complete": True}
        assert store.peers.apply_snapshot("BHOME", "home", good)
        assert not store.peers.apply_snapshot("BHOME", "home", {"kind": "snapshot",
            "entries": [{"route": "moranwm:agentbus:impostor", "display_name": "Nope"}],
            "complete": True})
        assert store.peers.list("moranwm", {"home": "BHOME"})["agents"][0]["route"] == "home:agentbus:weed"
    finally:
        store.close()


class FakeSocket:
    def __init__(self, _settings, _receiver):
        pass

    def connect(self):
        pass

    def close(self):
        pass


def test_socket_receivers_exchange_directory_without_user_message_or_wake(tmp_path):
    home = MessageStore(tmp_path / "home.sqlite3")
    remote = MessageStore(tmp_path / "remote.sqlite3")

    class Immediate:
        def submit(self, task, *args):
            task(*args)

    class SocketClient:
        def __init__(self):
            self.posts = []
            self.acks = []
            self.web_client = self

        def chat_postMessage(self, **kwargs):
            self.posts.append(kwargs)

        def send_socket_mode_response(self, response):
            self.acks.append(response.envelope_id)

    try:
        joined(home, "home:agentbus:weed", "weed-secret")
        settings = Settings(api_token="a" * 32, slack_bot_token="xoxb-test",
                            slack_app_token="xapp-test", slack_channel=CHANNEL,
                            db_path=tmp_path / "unused.sqlite3", local_prefix="home",
                            peer_bot_ids={"moranwm": "BREMOTE"})
        receiver = SlackReceiver(home, CHANNEL, "BHOME", settings)
        receiver._commands = Immediate()
        client = SocketClient()
        request = bot_event("BREMOTE", wire({"kind": "request", "nonce": str(uuid.uuid4())}))
        receiver(client, SocketModeRequest(type="events_api", envelope_id="req",
                                           payload={"event": request}))
        assert client.acks == ["req"] and len(client.posts) == 1
        assert home.read(CHANNEL).messages == []
        snapshot = bot_event("BHOME", client.posts[0]["text"])
        remote_settings = Settings(api_token="a" * 32, slack_bot_token="xoxb-test",
                                   slack_app_token="xapp-test", slack_channel=CHANNEL,
                                   db_path=tmp_path / "unused2.sqlite3", local_prefix="moranwm",
                                   peer_bot_ids={"home": "BHOME"})
        remote_receiver = SlackReceiver(remote, CHANNEL, "BREMOTE", remote_settings)
        remote_receiver(SocketClient(), SocketModeRequest(type="events_api", envelope_id="snapshot",
                                                          payload={"event": snapshot}))
        assert remote.peers.list("moranwm", {"home": "BHOME"})["agents"][0]["route"] == "home:agentbus:weed"
        assert remote.read(CHANNEL).messages == []
    finally:
        home.close()
        remote.close()


def test_peer_message_in_nondefault_room_reaches_global_inbox(tmp_path, monkeypatch, capsys):
    other = "C987654"
    store = MessageStore(tmp_path / "rooms.sqlite3")

    class Client:
        def __init__(self):
            self.acks = []

        def send_socket_mode_response(self, response):
            self.acks.append(response.envelope_id)

    try:
        joined(store, "moranwm:racecar:maintainer", "remote-secret")
        settings = Settings(api_token="a" * 32, slack_bot_token="xoxb-test",
                            slack_app_token="xapp-test", slack_channel=CHANNEL,
                            slack_channels=frozenset({other}),
                            slack_repo_channels={"racecar": other},
                            db_path=tmp_path / "unused.sqlite3", local_prefix="moranwm",
                            peer_bot_ids={"home": "BHOME"})
        sent = SendMessage(sender="home:agentbus:signal-gardener",
                           recipient="moranwm:racecar:maintainer", text="Cross-room work")
        sent._sender_assurance = "session"
        event = dict(bot_event("BHOME", encode_envelope(sent)), channel=other)
        receiver = SlackReceiver(store, CHANNEL, "BREMOTE", settings)
        client = Client()
        old_envelope = json.loads(encode_envelope(sent))
        old_envelope.pop("origin_assurance")
        old_event = dict(bot_event("BHOME", json.dumps(old_envelope), "1700000001.000000"),
                         channel=other)
        receiver(client, SocketModeRequest(type="events_api", envelope_id="old-peer",
                                           payload={"event": old_event}))
        legacy = store.read(other).messages[0]
        assert legacy.sender == "slack:BHOME" and legacy.recipient == "all"
        assert legacy.audience == "unrouted" and legacy.sender_assurance == "legacy"
        source = store.peers.list("moranwm", {"home": "BHOME"})["sources"][1]
        assert source["last_missing_origin_at"] == legacy.received_at
        monkeypatch.setattr(agentbus_peer_client, "api", lambda *_args: store.peers.list(
            "moranwm", {"home": "BHOME"}) | {"local_bot_id": "BREMOTE"})
        agentbus_peer_client.peers(SimpleNamespace(refresh=False, json=False), {})
        assert "old AgentBus envelope without origin proof observed" in capsys.readouterr().out
        receiver(client, SocketModeRequest(type="events_api", envelope_id="peer-room",
                                           payload={"event": event}))
        assert client.acks == ["old-peer", "peer-room"]
        from src.agentbus_multichannel_inbox_service import read_inbox
        page = read_inbox(settings, store, "moranwm:racecar:maintainer", 0, 100, None, False, None)
        assert len(page.messages) == 1
        assert page.messages[0].sender_assurance == "peer"
        assert page.messages[0].channel == other and page.messages[0].actionable
        assert len(store.read(other).messages) == 2
        observed = store.peers.list("moranwm", {"home": "BHOME"})["sources"][1]["last_missing_origin_at"]
        store.close()
        store = MessageStore(tmp_path / "rooms.sqlite3")
        assert store.peers.list("moranwm", {"home": "BHOME"})["sources"][1]["last_missing_origin_at"] == observed
    finally:
        store.close()


def test_refresh_requires_exact_joined_proof_and_is_bounded(tmp_path):
    posts = []

    def respond(request):
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json={"ok": True, "bot_id": "BHOME"})
        posts.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "channel": CHANNEL,
                                         "ts": "1700000001.000001"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    settings = Settings(api_token="a" * 32, slack_bot_token="xoxb-fake",
                        slack_app_token="xapp-fake", slack_channel=CHANNEL,
                        db_path=tmp_path / "messages.sqlite3", local_prefix="home",
                        peer_bot_ids={"moranwm": "BREMOTE"})
    app = create_app(settings, http_client=http, socket_factory=FakeSocket)
    with TestClient(app) as client:
        joined(app.state.store, "home:agentbus:weed", "weed-secret")
        headers = {"Authorization": "Bearer " + "a" * 32}
        assert client.get("/v1/peers").status_code == 401
        listing = client.get("/v1/peers", headers=headers).json()
        assert listing["agents"][0]["route"] == "home:agentbus:weed"
        assert listing["sources"][1]["observed_at"] is None
        path = "/v1/peers/refresh"
        body = {"identity": "home:agentbus:weed"}
        assert client.post(path, headers=headers, json=body).status_code == 403
        proved = dict(headers, **{"X-AgentBus-Session-Token": "weed-secret" * 4})
        assert client.post(path, headers=proved, json=body).status_code == 200
        assert json.loads(posts[0]["text"])["kind"] == "request"
        limited = client.post(path, headers=proved, json=body)
        assert limited.status_code == 429 and limited.headers["Retry-After"] == "30"
        assert len(posts) == 1
    import asyncio
    asyncio.run(http.aclose())


def test_peer_bot_binding_requires_distinct_prefix_and_valid_bot_id(monkeypatch, tmp_path):
    for name, value in {
        "AGENTBUS_API_TOKEN": "a" * 32,
        "AGENTBUS_SLACK_BOT_TOKEN": "xoxb-test",
        "AGENTBUS_SLACK_APP_TOKEN": "xapp-test",
        "AGENTBUS_SLACK_CHANNEL": CHANNEL,
        "AGENTBUS_DB_PATH": str(tmp_path / "state.sqlite3"),
        "AGENTBUS_LOCAL_PREFIX": "home",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("AGENTBUS_PEER_BOT_IDS", '{"moranwm":"BREMOTE"}')
    assert Settings.from_env().peer_bot_ids == {"moranwm": "BREMOTE"}
    for invalid in ('{"home":"BOTHER"}', '{"moranwm":"UUSER"}',
                    '{"moranwm":"BREMOTE","second":"BREMOTE"}'):
        monkeypatch.setenv("AGENTBUS_PEER_BOT_IDS", invalid)
        with pytest.raises(ValueError):
            Settings.from_env()
