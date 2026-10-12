"""Room placement and recovery controls for the single durable AgentBus graph."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from slack_sdk.socket_mode.request import SocketModeRequest

from src.agentbus_service import MessageStore, SendMessage, SlackReceiver, create_app
from src.agentbus_settings_service import Settings
from src.agentbus_slack_command_service import execute_command

DEFAULT = "C123456"
OTHER = "C987654"
AUTH = {"Authorization": "Bearer " + "a" * 32}
SECRET = "session-secret-with-at-least-32-characters"
POLICY = {
    "initial_interval_seconds": 60, "backoff_factor": 2,
    "max_interval_seconds": 1920, "control_check_max_seconds": 60,
    "jitter_fraction": 0, "overdue_grace_seconds": 120,
    "presentation_budget_bytes": None,
}


class FakeSocket:
    def __init__(self, settings, receiver):
        self.receiver = receiver
        self.web_client = None
        self.acks = []

    def connect(self):
        return None

    def close(self):
        return None

    def send_socket_mode_response(self, response):
        self.acks.append(response.envelope_id)


def settings(tmp_path):
    return Settings(api_token="a" * 32, slack_bot_token="xoxb-test", slack_app_token="xapp-test",
                    slack_channel=DEFAULT, slack_channels=frozenset({OTHER}),
                    slack_repo_channels={"racecar": OTHER}, db_path=tmp_path / "messages.sqlite3")


def test_room_configuration_requires_explicit_allowed_ids(monkeypatch, tmp_path):
    for key, value in {
        "AGENTBUS_API_TOKEN": "a" * 32,
        "AGENTBUS_SLACK_BOT_TOKEN": "xoxb-test",
        "AGENTBUS_SLACK_APP_TOKEN": "xapp-test",
        "AGENTBUS_SLACK_CHANNEL": DEFAULT,
        "AGENTBUS_SLACK_CHANNELS": OTHER,
        "AGENTBUS_SLACK_REPO_CHANNELS": json.dumps({"racecar": OTHER}),
        "AGENTBUS_DB_PATH": str(tmp_path / "state.sqlite3"),
    }.items():
        monkeypatch.setenv(key, value)
    assert Settings.from_env().slack_repo_channels == {"racecar": OTHER}
    monkeypatch.setenv("AGENTBUS_SLACK_REPO_CHANNELS", json.dumps({"racecar": "CUNKNOWN"}))
    with pytest.raises(ValueError, match="configured channel IDs"):
        Settings.from_env()
    monkeypatch.setenv("AGENTBUS_SLACK_REPO_CHANNELS", json.dumps({"Slack": OTHER}))
    with pytest.raises(ValueError, match="repository names"):
        Settings.from_env()


def enroll(store, route, secret=SECRET):
    _, session, revision = store.participation.enroll(
        repo=route.split(":")[1], route=route, display_name=route, session_secret=secret)
    store.participation.acknowledge_policy(session, secret, revision)


def test_room_placement_reply_ancestry_and_global_inbox(tmp_path):
    config = settings(tmp_path)
    posts = []

    def respond(request):
        if request.url.path == "/api/auth.test":
            return httpx.Response(200, json={"ok": True, "bot_id": "B123"})
        payload = json.loads(request.content)
        posts.append(payload)
        return httpx.Response(200, json={"ok": True, "channel": payload["channel"],
                                         "ts": f"1700000000.{len(posts):06}"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    with TestClient(create_app(config, http_client=http, socket_factory=FakeSocket)) as client:
        store = client.app.state.store
        store.participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
        enroll(store, "home:agentbus:flower")
        torque_secret = SECRET + "-torque"
        enroll(store, "home:racecar:torque", torque_secret)
        proof = {**AUTH, "X-AgentBus-Session-Token": SECRET}
        torque_proof = {**AUTH, "X-AgentBus-Session-Token": torque_secret}
        first = client.post("/v1/messages", headers=proof, json={
            "sender": "home:agentbus:flower", "recipient": "home:racecar:torque",
            "text": "For torque", "audience": "direct",
        })
        assert first.status_code == 201, first.text
        assert first.json()["channel"] == OTHER
        broadcast = client.post("/v1/messages", headers=proof, json={
            "sender": "home:agentbus:flower", "recipient": "all", "audience": "broadcast",
            "text": "For my room",
        })
        assert broadcast.status_code == 201 and broadcast.json()["channel"] == DEFAULT
        foreign_broadcast = client.post("/v1/messages", headers=torque_proof, json={
            "sender": "home:racecar:torque", "recipient": "all", "audience": "broadcast",
            "text": "Not for flower's room",
        })
        assert foreign_broadcast.status_code == 201 and foreign_broadcast.json()["channel"] == OTHER
        reply = client.post("/v1/messages", headers=torque_proof, json={
            "sender": "home:racecar:torque", "text": "Reply in parent room",
            "reply_to_cursor": first.json()["cursor"],
        })
        assert reply.status_code == 201, reply.text
        assert reply.json()["channel"] == OTHER
        assert reply.json()["recipient"] == "home:agentbus:flower"
        assert [post["channel"] for post in posts] == [OTHER, DEFAULT, OTHER, OTHER]
        flower = client.get("/v1/inbox", headers=proof,
                            params={"identity": "home:agentbus:flower"}).json()
        assert [m["text"] for m in flower["messages"]] == ["For my room", "Reply in parent room"]
        assert flower["next_cursor"] == reply.json()["cursor"]
        foreign_context = client.get("/v1/inbox", headers=proof, params={
            "identity": "home:agentbus:flower", "channel": OTHER,
        }).json()
        foreign_broadcast_context = next(item for item in foreign_context["messages"]
                                         if item["text"] == "Not for flower's room")
        assert foreign_broadcast_context["actionable"] is False
        assert foreign_broadcast_context["action_reason"] == "broadcast in another room"
        claimable = store.append(OTHER, "1700000000.000099", SendMessage(
            sender="slack:U123", text="Needs a claimant", audience="unrouted"))
        claimed = client.post(f"/v1/messages/{claimable.cursor}/claim", headers=proof,
                              json={"identity": "home:agentbus:flower"})
        assert claimed.status_code == 200 and claimed.json()["cursor"] == claimable.cursor
        newer = client.get("/v1/inbox", headers=proof, params={
            "identity": "home:agentbus:flower", "after": reply.json()["cursor"],
        }).json()
        assert [m["text"] for m in newer["messages"]] == ["Needs a claimant"]
        assert newer["messages"][0]["channel"] == OTHER
        assert client.get("/v1/info", headers=AUTH).json()["channel"] == DEFAULT
        assert client.get("/v1/messages", headers=AUTH, params={"channel": OTHER}).status_code == 200
        assert client.get("/v1/messages", headers=AUTH, params={"channel": "CUNKNOWN"}).status_code == 422
        assert client.post("/v1/messages", headers=proof, json={
            "sender": "home:agentbus:flower", "recipient": "home:racecar:torque",
            "channel": DEFAULT, "text": "No caller room authority",
        }).status_code == 422
        store.append(DEFAULT, "1700000000.000200", SendMessage(
            sender="home:agentbus:flower", text="Root one", audience="broadcast"))
        store.append(OTHER, "1700000000.000200", SendMessage(
            sender="home:racecar:torque", text="Root two", audience="broadcast"))
        assert client.get("/v1/inbox", headers=proof, params={
            "identity": "home:agentbus:flower", "thread_ts": "1700000000.000200",
        }).status_code == 422
        assert client.get("/v1/inbox", headers=proof, params={
            "identity": "home:agentbus:flower", "thread_ts": "1700000000.000200",
            "channel": DEFAULT,
        }).status_code == 200
    asyncio.run(http.aclose())


def test_thread_update_recovers_missing_human_reply_before_ack(tmp_path):
    config = settings(tmp_path)
    store = MessageStore(config.db_path)
    store.participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    enroll(store, "home:racecar:torque")
    root = SendMessage(sender="home:racecar:torque", recipient="all", audience="broadcast",
                       text="Racecar finding")
    root._sender_assurance = "session"
    stored = store.append(OTHER, "1700000000.000001", root)
    receiver = SlackReceiver(store, DEFAULT, "B123", config)
    socket = FakeSocket(config, receiver)

    class Web:
        fail = False
        omit_root = False

        def conversations_replies(self, **kwargs):
            assert kwargs["channel"] == OTHER and kwargs["ts"] == stored.slack_ts
            if self.fail:
                raise RuntimeError("upstream unavailable")
            if self.omit_root:
                return {"ok": True, "has_more": False, "messages": [
                    {"ts": "1700000000.000003", "thread_ts": stored.slack_ts,
                     "user": "U123", "text": "Orphaned page"},
                ]}
            return {"ok": True, "has_more": False, "messages": [
                {"ts": stored.slack_ts, "text": "Racecar finding"},
                {"ts": "1700000000.000002", "thread_ts": stored.slack_ts,
                 "user": "U123", "text": "Question for Racecar"},
            ]}

    socket.web_client = Web()
    update = {"type": "message", "subtype": "message_replied", "hidden": True,
              "channel": OTHER, "message": {"ts": stored.slack_ts}}
    request = SocketModeRequest(type="events_api", envelope_id="update-1", payload={"event": update})
    socket.receiver(socket, request)
    assert socket.acks == ["update-1"]
    recovered = store.message_by_ts(OTHER, "1700000000.000002")
    assert (recovered.sender, recovered.sender_assurance, recovered.recipient) == (
        "slack:U123", "slack-human", "home:racecar:torque")
    assert recovered.reply_to_cursor == stored.cursor
    socket.receiver(socket, request)
    assert len(store.read(OTHER).messages) == 2
    no_subtype = {"type": "message", "hidden": True, "channel": OTHER,
                  "message": {"ts": stored.slack_ts, "reply_count": 1}}
    socket.receiver(socket, SocketModeRequest(
        type="events_api", envelope_id="update-hidden", payload={"event": no_subtype}))
    assert socket.acks[-1] == "update-hidden"
    assert len(store.read(OTHER).messages) == 2
    socket.web_client.fail = True
    failed = SocketModeRequest(type="events_api", envelope_id="update-2", payload={"event": update})
    socket.receiver(socket, failed)
    assert socket.acks == ["update-1", "update-1", "update-hidden"]
    socket.web_client.fail = False
    socket.web_client.omit_root = True
    socket.receiver(socket, SocketModeRequest(
        type="events_api", envelope_id="update-without-root", payload={"event": update}))
    assert "update-without-root" not in socket.acks
    ordinary = {"type": "message", "channel": OTHER, "ts": "1700000000.000004",
                "user": "U123", "text": "Ordinary room message"}
    socket.receiver(socket, SocketModeRequest(
        type="events_api", envelope_id="ordinary-other-room", payload={"event": ordinary}))
    assert socket.acks[-1] == "ordinary-other-room"
    assert store.message_by_ts(OTHER, ordinary["ts"]).sender == "slack:U123"
    assert store.message_by_ts(DEFAULT, ordinary["ts"]) is None
    store.close()


def test_operator_command_from_allowed_room_targets_session_room(tmp_path):
    config = replace(settings(tmp_path), slack_operator_user_ids=frozenset({"U123"}))
    store = MessageStore(config.db_path)
    store.participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    _, session, revision = store.participation.enroll(
        repo="racecar", route="home:racecar:torque", display_name="Torque",
        session_secret=SECRET)
    store.participation.acknowledge_policy(session, SECRET, revision)

    class Web:
        def __init__(self):
            self.posts = []

        def chat_postMessage(self, **kwargs):
            self.posts.append(kwargs)
            return {"ok": True, "channel": kwargs["channel"], "ts": "1700000000.000003"}

    web = Web()
    payload = {"command": "/agentbus", "channel_id": DEFAULT, "user_id": "U123",
               "text": f"wake {session[-6:]} Please check in"}
    result = execute_command(payload, config, store, web)
    assert OTHER in result and len(web.posts) == 1
    assert web.posts[0]["channel"] == OTHER
    assert store.read(OTHER).messages[0].recipient == "home:racecar:torque"
    assert "configured channel" in execute_command(
        {**payload, "channel_id": "CUNKNOWN"}, config, store, web)
    assert "not enabled" in execute_command(
        {**payload, "user_id": "U999"}, config, store, web)
    assert len(web.posts) == 1
    store.close()
