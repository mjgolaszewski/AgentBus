"""Isolated A2A1.0 clients share existing custody and never claim agent execution."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from src.agentbus_a2a_service import A2AStore
from src.agentbus_service import MessageStore, Settings, create_app


@pytest.fixture
def adapter(tmp_path):
    store = MessageStore(tmp_path / "bus.db")
    store.channels.configure(("C111", "C222"))
    store.lifecycle.activate_fresh(instant=None)
    store.participation.initialize_default_policy()
    principals, secrets = {}, {}
    for name, channel in (("writer", "C111"), ("reader", "C111"), ("foreign", "C222")):
        route, secret = "alpha:agentbus:" + name, (name + "-private-").ljust(40, "x")
        _, session, revision = store.participation.enroll(repo="agentbus", route=route, display_name=name,
                                                        session_secret=secret, channel=channel)
        store.participation.acknowledge_policy(session, secret, revision)
        principals[name], secrets[name] = store.channels.principal(secret, route), secret
    settings = Settings(api_token="a2a-test-bearer", slack_bot_token="xoxb-test", slack_app_token="xapp-test",
                        slack_channel="", slack_channels=("C111", "C222"), db_path=tmp_path / "bus.db", a2a_enabled=True)
    app = create_app(settings)
    app.state.store, app.state.a2a = store, A2AStore(store)
    app.state.delivery = SimpleNamespace(wake=asyncio.Event())
    client = TestClient(app, base_url="http://127.0.0.1:8766", client=("127.0.0.1", 12345))
    def headers(name="writer"):
        return {"Authorization": "Bearer a2a-test-bearer", "X-AgentBus-Session-Token": secrets[name],
                "X-AgentBus-Inbox-ID": principals[name].inbox_id, "X-AgentBus-Lifecycle-Version": "1",
                "X-AgentBus-Request-Epoch": store.lifecycle.epoch(principals[name].channel)["epoch"], "A2A-Version": "1.0"}
    def rpc(method="SendMessage", params=None, name="writer", extra=None):
        envelope = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return client.post("/a2a", headers={**headers(name), **(extra or {})}, json=envelope)
    yield store, principals, client, headers, rpc
    client.close()
    store.close()


def intent(text="An explicit mailbox task", key=None):
    return {"message": {"messageId": key or str(uuid4()), "role": "ROLE_USER", "parts": [{"text": text}],
                        "metadata": {"operation": "mailbox.accept", "recipient": "alpha:agentbus:reader"}}}


def test_a2a_admission_completion_is_not_recipient_execution(adapter):
    store, _, _, _, rpc = adapter
    params = intent()
    first = rpc(params=params)
    assert first.status_code == 200, first.text
    task = first.json()["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    facts = task["artifacts"][0]["parts"][0]["data"]
    assert facts["accepted"] and facts["boundary"] == "origin durable custody"
    assert facts["recipientWork"] == facts["semanticAcknowledgement"] == "unknown"
    assert rpc(params=params).json()["result"]["task"]["id"] == task["id"]
    assert store._db.execute("SELECT count(*) FROM local_delivery_obligations").fetchone()[0] == 1
    assert store._db.execute("SELECT count(*) FROM a2a_tasks").fetchone()[0] == 1
    assert rpc("GetTask", {"id": task["id"]}).json()["result"]["id"] == task["id"]
    assert rpc("GetTask", {"id": task["id"]}, name="foreign").json()["error"]["code"] == -32001
    assert rpc("GetTask", {"id": task["id"]}, name="reader").json()["error"]["code"] == -32001
    assert rpc("CancelTask", {"id": task["id"]}).json()["error"]["code"] == -32002
    changed = intent("Changed", params["message"]["messageId"])
    assert rpc(params=changed).json()["error"]["code"] == -32602


def test_a2a_envelopes_scope_and_unsupported_paths_fail_before_custody(adapter):
    store, principals, client, headers, rpc = adapter
    for version in ("", "0.3", "1.1"):
        assert rpc(params=intent(), extra={"A2A-Version": version}).json()["error"]["code"] == -32009
    assert client.post("/a2a", json={}).status_code == 401
    assert client.post("/a2a", headers=headers(), content="[").json()["error"]["code"] == -32700
    assert rpc("SendStreamingMessage", intent()).json()["error"]["code"] == -32004
    assert rpc("CreateTaskPushNotificationConfig", {"url": "https://invalid.example/callback"}).json()["error"]["code"] == -32003
    for mutation in ({"parts": [{"url": "https://invalid.example/secret"}]}, {"role": "ROLE_AGENT"},
                     {"taskId": str(uuid4())}, {"metadata": {"operation": "execute", "recipient": "all"}}):
        params = intent()
        params["message"].update(mutation)
        assert rpc(params=params).json()["error"]["code"] == -32602
    assert rpc(params=intent(), extra={"X-AgentBus-Inbox-ID": principals["foreign"].inbox_id}).status_code == 403
    store._db.execute("UPDATE participation_sessions SET state='stopped' WHERE session_id=?", (principals["writer"].session_id,))
    store._db.commit()
    assert rpc(params=intent()).status_code == 423
    assert store._db.execute("SELECT count(*) FROM a2a_tasks").fetchone()[0] == 0
    assert store._db.execute("SELECT count(*) FROM local_delivery_obligations").fetchone()[0] == 0


def test_a2a_private_card_and_pagination_are_scope_bound(adapter):
    _, _, _, _, rpc = adapter
    card = rpc("GetExtendedAgentCard").json()["result"]
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert not card["capabilities"]["streaming"] and not card["capabilities"]["pushNotifications"]
    assert "roster" not in str(card).lower()
    ids = {rpc(params=intent()).json()["result"]["task"]["id"] for _ in range(3)}
    first = rpc("ListTasks", {"pageSize": 2}).json()["result"]
    second = rpc("ListTasks", {"pageSize": 2, "pageToken": first["nextPageToken"]}).json()["result"]
    assert {task["id"] for task in first["tasks"] + second["tasks"]} == ids
    assert not second["nextPageToken"]
    assert rpc("ListTasks", name="foreign").json()["result"]["totalSize"] == 0
    assert rpc("ListTasks", {"pageToken": first["nextPageToken"]}, name="foreign").json()["error"]["code"] == -32001


def test_a2a_task_index_failure_rolls_back_existing_mailbox_admission(adapter):
    store, _, _, _, rpc = adapter
    store._db.execute("CREATE TRIGGER reject_a2a BEFORE INSERT ON a2a_tasks BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    store._db.commit()
    response = rpc(params=intent())
    assert response.status_code == 503
    assert "fixture failure" not in response.text
    assert store._db.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    assert store._db.execute("SELECT count(*) FROM local_delivery_obligations").fetchone()[0] == 0


def test_a2a_disabled_or_nonloopback_is_unavailable(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        Settings(api_token="test", slack_bot_token="xoxb-test", slack_app_token="xapp-test", slack_channel="C111",
                 db_path=tmp_path / "bus.db", a2a_enabled=True, bind_host="0.0.0.0")
    settings = Settings(api_token="test", slack_bot_token="xoxb-test", slack_app_token="xapp-test", slack_channel="C111",
                        db_path=tmp_path / "bus.db")
    client = TestClient(create_app(settings))
    try:
        assert client.post("/a2a", headers={"Authorization": "Bearer test"}, json={}).status_code == 404
    finally:
        client.close()
