"""Explicit publication custody never forwards the ordinary bus or borrows scope."""
import copy
from uuid import uuid4

import pytest

from src.agentbus_publication_service import PublicationPolicy, PublicationStore
from src.agentbus_service import MessageStore


@pytest.fixture
def publication(tmp_path):
    owner = MessageStore(tmp_path / "bus.db")
    owner.channels.configure(("C111", "C222"))
    owner.participation.initialize_default_policy()
    route, secret = "alpha:agentbus:writer", "writer-" + "x" * 40
    _, session, revision = owner.participation.enroll(
        repo="agentbus", route=route, display_name="writer", session_secret=secret, channel="C111")
    owner.participation.acknowledge_policy(session, secret, revision)
    principal = owner.channels.principal(secret, route)
    config = {"version": 1, "installations": [{"name": "client", "team_id": "T123", "bot_id": "B123",
               "token_env": "CLIENT_TOKEN"}], "destinations": [{"name": "updates", "installation": "client",
               "channel": "C999", "source_channels": ["C111"], "authors": [route],
               "approvers": ["U123"], "direct_authors": [], "enabled": True, "label": "Project update"}]}
    policy = PublicationPolicy(config, channels=("C111", "C222"), operators=("U123",),
                               environ={"CLIENT_TOKEN": "xoxb-private-fixture"})
    custody = PublicationStore(owner, policy)
    custody.bind_coordination("T123")
    yield owner, custody, principal, config
    owner.close()


def test_draft_approval_binds_exact_content_actor_and_policy(publication):
    _, custody, principal, _ = publication
    request = str(uuid4())
    draft = custody.admit(principal, request, "updates", "A deliberate update")
    assert draft["state"] == "awaiting_approval" and draft["request_id"] == request
    assert custody.admit(principal, request, "updates", "A deliberate update") == draft
    with pytest.raises(ValueError):
        custody.admit(principal, request, "updates", "Altered duplicate")
    with pytest.raises(PermissionError):
        custody.approve(draft["publication_id"], "UOTHER", draft["digest"])
    with pytest.raises(ValueError):
        custody.approve(draft["publication_id"], "U123", "0" * 64)
    receipt = custody.approve(draft["publication_id"], "U123", draft["digest"])
    assert receipt["state"] == "ready" and receipt["approval_actor"] == "U123"
    assert custody.approve(draft["publication_id"], "U123", draft["digest"]) == receipt


def test_publication_denies_foreign_or_revoked_scope_without_disclosure(publication):
    owner, custody, principal, _ = publication
    for name in ("unknown", "updates"):
        from dataclasses import replace
        with pytest.raises(PermissionError, match="Publication destination is unavailable"):
            custody.admit(replace(principal, channel="C222"), str(uuid4()), name, "Denied")
    request = str(uuid4())
    draft = custody.admit(principal, request, "updates", "Approved then revoked")
    custody.approve(draft["publication_id"], "U123", draft["digest"])
    owner._db.execute("UPDATE participation_sessions SET state='stopped' WHERE session_id=?", (principal.session_id,))
    owner._db.commit()
    assert custody.begin(draft["publication_id"]) is None
    assert custody.status(principal, request)["state"] == "failed"


def test_uncertain_submission_survives_restart_without_blind_retry_or_cancel(publication):
    owner, custody, principal, _ = publication
    request = str(uuid4())
    draft = custody.admit(principal, request, "updates", "Once")
    custody.approve(draft["publication_id"], "U123", draft["digest"])
    assert custody.begin(draft["publication_id"]) is not None
    assert custody.begin(draft["publication_id"]) is None
    reopened = PublicationStore(owner, custody.policy)
    assert reopened.status(principal, request)["state"] == "uncertain"
    with pytest.raises(ValueError):
        reopened.cancel(principal, request)
    assert reopened.begin(draft["publication_id"]) is None


def test_direct_policy_is_explicit_and_revision_changes_invalidate_approval(publication):
    _, custody, principal, config = publication
    request = str(uuid4())
    draft = custody.admit(principal, request, "updates", "Old policy")
    custody.approve(draft["publication_id"], "U123", draft["digest"])
    changed = copy.deepcopy(config)
    changed["destinations"][0]["direct_authors"] = [principal.route]
    custody.policy = PublicationPolicy(changed, channels=("C111", "C222"), operators=("U123",),
                                       environ={"CLIENT_TOKEN": "xoxb-private-fixture"})
    assert custody.begin(draft["publication_id"]) is None
    assert custody.status(principal, request)["state"] == "failed"
    direct = custody.admit(principal, str(uuid4()), "updates", "Explicit direct policy")
    assert direct["state"] == "ready" and direct["approval_actor"] is None


def test_pre_submission_cancel_and_foreign_receipt_deny(publication):
    _, custody, principal, _ = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "Cancel before posting")
    assert custody.cancel(principal, draft["request_id"])["state"] == "cancelled"
    assert custody.begin(draft["publication_id"]) is None
    from dataclasses import replace
    with pytest.raises(PermissionError):
        custody.status(replace(principal, chat_id="foreign"), draft["request_id"])


def test_disabled_destination_cannot_be_approved(publication):
    _, custody, principal, config = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "Disabled before approval")
    changed = copy.deepcopy(config)
    changed["destinations"][0]["enabled"] = False
    custody.policy = PublicationPolicy(changed, channels=("C111", "C222"), operators=("U123",),
                                       environ={"CLIENT_TOKEN": "xoxb-private-fixture"})
    with pytest.raises(PermissionError, match="Publication destination is unavailable"):
        custody.approve(draft["publication_id"], "U123", draft["digest"])


def test_expired_ready_draft_fails_without_starting_provider_submission(publication):
    _, custody, principal, _ = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "Approved but delayed")
    custody.approve(draft["publication_id"], "U123", draft["digest"])
    custody.now = lambda: draft["created"] + 86401
    assert custody.begin(draft["publication_id"]) is None
    assert custody.status(principal, draft["request_id"])["failure"] == "expired"


def test_publication_epoch_and_author_pages_bind_exact_scope(publication):
    _, custody, principal, _ = publication
    from dataclasses import replace
    epoch = custody.request_epoch(principal.channel)
    assert len(epoch) == 64 and epoch != custody.request_epoch("C222")
    custody.validate_epoch(principal, epoch)
    with pytest.raises(PermissionError):
        custody.validate_epoch(principal, custody.request_epoch("C222"))
    first = custody.admit(principal, str(uuid4()), "updates", "First")
    second = custody.admit(principal, str(uuid4()), "updates", "Second")
    page = custody.list_author(principal, limit=1)
    assert [r["request_id"] for r in page["publications"]] == [second["request_id"]]
    following = custody.list_author(principal, after=page["next_after"], limit=1)
    assert [r["request_id"] for r in following["publications"]] == [first["request_id"]]
    assert following["has_more"] is False
    with pytest.raises(PermissionError):
        custody.list_author(replace(principal, chat_id="foreign"), after=first["request_id"])


def test_operator_publication_lookup_is_actor_and_channel_scoped(publication):
    _, custody, principal, _ = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "Private review")
    assert custody.list_operator("U123", "C111")["publications"][0]["request_id"] == draft["request_id"]
    assert custody.list_operator("UOTHER", "C111")["publications"] == []
    assert custody.list_operator("U123", "C222")["publications"] == []
    with pytest.raises(PermissionError):
        custody.operator_status(draft["publication_id"], "U123", "C222")


@pytest.mark.parametrize("outcome", ["delivered", "wrong_channel", "lost_response"])
def test_bound_provider_post_and_uncertainty_never_blindly_retry(publication, outcome):
    import asyncio
    import json

    import httpx

    from src.agentbus_publication_runtime_service import PublicationWorker

    _, custody, principal, _ = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "Deliberate client text")
    custody.approve(draft["publication_id"], "U123", draft["digest"])
    requests = []

    def respond(request):
        requests.append((request.url.path, request.headers["authorization"]))
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json={"ok": True, "team_id": "T123", "bot_id": "B123"})
        body = json.loads(request.content)
        assert body["channel"] == "C999" and body["client_msg_id"] == draft["publication_id"]
        assert "alpha:agentbus:writer" not in body["text"]
        if outcome == "lost_response":
            raise httpx.ReadTimeout("response lost")
        return httpx.Response(200, json={"ok": True, "channel": "COTHER" if outcome == "wrong_channel" else "C999",
                                        "ts": "100.1", "message": {"text": body["text"]}})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            worker = PublicationWorker(custody, client)
            assert await worker.submit_once(budget=1) == 1
            assert custody.status(principal, draft["request_id"])["state"] == "ready"
            assert await worker.submit_once(budget=1) == 1
            assert await worker.submit_once(budget=1) == 0
    asyncio.run(run())
    receipt = custody.status(principal, draft["request_id"])
    assert receipt["state"] == ("delivered" if outcome == "delivered" else "uncertain")
    assert len(requests) == 2 and all(token == "Bearer xoxb-private-fixture" for _, token in requests)


def test_wrong_workspace_denies_before_any_client_post(publication):
    import asyncio

    import httpx

    from src.agentbus_publication_runtime_service import PublicationWorker

    _, custody, principal, _ = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "No wrong workspace")
    custody.approve(draft["publication_id"], "U123", draft["digest"])
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "team_id": "TOTHER", "bot_id": "B123"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await PublicationWorker(custody, client).submit_once(budget=1) == 1
    asyncio.run(run())
    assert calls == ["/api/auth.test"]
    assert custody.status(principal, draft["request_id"])["state"] == "failed"


@pytest.fixture
def publication_http(tmp_path, monkeypatch):
    import asyncio
    import json

    import httpx
    from fastapi.testclient import TestClient

    from src.agentbus_service import Settings, create_app

    class Socket:
        def connect(self):
            pass
        def close(self):
            pass
        def is_connected(self):
            return True

    monkeypatch.setenv("CLIENT_TOKEN", "xoxb-private-fixture")
    config = {"version": 1, "installations": [{"name": "client", "team_id": "T123", "bot_id": "B123",
               "token_env": "CLIENT_TOKEN"}], "destinations": [{"name": "updates", "installation": "client",
               "channel": "C999", "source_channels": ["C111"], "authors": ["alpha:agentbus:writer"],
               "approvers": ["U123"], "direct_authors": [], "enabled": True}]}
    path = tmp_path / "publication.json"
    path.write_text(json.dumps(config))
    settings = Settings(api_token="api-test", slack_bot_token="xoxb-coordination-fixture",
                        slack_app_token="xapp-fixture", slack_channel="", slack_channels=("C111", "C222"),
                        slack_operator_user_ids=frozenset({"U123"}), db_path=tmp_path / "http.db",
                        publication_config=path)
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "team_id": "T123", "bot_id": "B123"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app = create_app(settings, http_client=http, socket_factory=lambda *_: Socket())
    with TestClient(app) as client:
        owner = app.state.store
        secret = "writer-" + "x" * 40
        _, session, revision = owner.participation.enroll(repo="agentbus", route="alpha:agentbus:writer",
            display_name="writer", session_secret=secret, channel="C111")
        owner.participation.acknowledge_policy(session, secret, revision)
        headers = {"Authorization": "Bearer api-test", "X-AgentBus-Session-Token": secret,
                   "X-AgentBus-Inbox-ID": owner.channels.inbox_id("C111")}
        yield client, app, headers, calls
    asyncio.run(http.aclose())


def test_http_publication_scope_epoch_idempotency_and_stopped_receipts(publication_http):
    client, app, headers, calls = publication_http
    info = client.get("/v1/info", headers=headers).json()
    assert "client-publication-v1" in info["features"]
    request_id = str(uuid4())
    delivery = {**headers, "Idempotency-Key": request_id, "X-AgentBus-Request-Epoch": info["publication"]["request_epoch"]}
    body = {"destination": "updates", "text": "Explicit update"}
    accepted = client.post("/v1/publications", json=body, headers=delivery)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["state"] == "awaiting_approval"
    assert client.post("/v1/publications", json=body, headers=delivery).json() == accepted.json()
    assert client.post("/v1/publications", json={**body, "text": "Changed"}, headers=delivery).status_code == 409
    assert client.post("/v1/publications", json=body, headers={**delivery, "X-AgentBus-Request-Epoch": "0" * 64}).status_code == 403
    assert client.post("/v1/publications", json=body, headers={**delivery, "X-AgentBus-Inbox-ID": "foreign"}).status_code == 403
    assert client.get("/v1/publications", headers=headers).json()["publications"][0]["request_id"] == request_id
    app.state.store._db.execute("UPDATE participation_sessions SET state='stopped'")
    app.state.store._db.commit()
    assert client.post("/v1/publications", json=body, headers=delivery).status_code == 423
    assert client.get("/v1/publications/" + request_id, headers=headers).status_code == 200
    assert client.get("/v1/messages", headers=headers).status_code == 423
    assert calls == ["/api/auth.test"]


def test_publication_capacity_preserves_existing_retry_and_rate_budget(publication, monkeypatch):
    from src import agentbus_publication_service as module
    _, custody, principal, _ = publication
    monkeypatch.setattr(module, "MAX_PUBLICATIONS", 1)
    draft = custody.admit(principal, str(uuid4()), "updates", "Retain original")
    assert custody.admit(principal, draft["request_id"], "updates", "Retain original") == draft
    with pytest.raises(module.PublicationCapacityError):
        custody.admit(principal, str(uuid4()), "updates", "No allocation")
    row = custody.owner._db.execute("SELECT used FROM send_rate_buckets WHERE principal=?",
                                    ("session:" + principal.session_id,)).fetchone()
    assert row["used"] == 1
    assert custody.owner._db.execute("SELECT count(*) FROM publication_requests").fetchone()[0] == 1


def test_known_provider_rate_rejection_retries_same_request_after_backoff(publication):
    import asyncio
    import json

    import httpx

    from src.agentbus_publication_runtime_service import PublicationWorker

    _, custody, principal, _ = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "Retry only known rejection")
    custody.approve(draft["publication_id"], "U123", draft["digest"])
    posts = []
    def respond(request):
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json={"ok": True, "team_id": "T123", "bot_id": "B123"})
        body = json.loads(request.content)
        posts.append(body)
        if len(posts) == 1:
            return httpx.Response(429, headers={"Retry-After": "60"}, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "channel": "C999", "ts": "100.1",
                                        "message": {"text": body["text"]}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            worker = PublicationWorker(custody, client)
            await worker.submit_once()
            await worker.submit_once()
            assert custody.status(principal, draft["request_id"])["state"] == "ready"
            assert await worker.submit_once() == 0
            before = custody.now
            custody.now = lambda: before() + 61
            await worker.submit_once()
    asyncio.run(run())
    assert len(posts) == 2 and posts[0] == posts[1]
    assert custody.status(principal, draft["request_id"])["state"] == "delivered"


def test_ordinary_bus_and_unapproved_drafts_produce_zero_publication_calls(publication):
    import asyncio

    import httpx

    from src.agentbus_publication_runtime_service import PublicationWorker
    from src.agentbus_service import SendMessage

    owner, custody, principal, _ = publication
    custody.admit(principal, str(uuid4()), "updates", "Still awaiting approval")
    owner.append("C111", "100.1", SendMessage(sender="human", text="Ordinary bus chatter"))
    def deny(request):
        raise AssertionError("Ordinary bus traffic must never enter the publication adapter")
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(deny)) as client:
            assert await PublicationWorker(custody, client).submit_once() == 0
    asyncio.run(run())


def test_disabled_publication_configuration_has_no_store_or_periodic_calls(publication_http, tmp_path):
    import asyncio
    from dataclasses import replace

    import httpx
    from fastapi.testclient import TestClient

    from src.agentbus_service import create_app

    _, existing, _, _ = publication_http
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ok": True, "bot_id": "B123"})
    settings = replace(existing.state.settings, publication_config=None, db_path=tmp_path / "disabled.db")
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app = create_app(settings, http_client=http, socket_factory=lambda *_: type(existing.state.socket)())
    with TestClient(app):
        assert app.state.publications is None and app.state.delivery.publications is None
        assert app.state.store._db.execute("SELECT count(*) FROM sqlite_master WHERE name='publication_requests'").fetchone()[0] == 0
    asyncio.run(http.aclose())
    assert calls == ["/api/auth.test"]


def test_shared_provider_cooldown_survives_restart_and_blocks_another_draft(publication):
    import asyncio

    import httpx

    from src.agentbus_publication_runtime_service import PublicationWorker

    owner, custody, principal, _ = publication
    for text in ("First draft", "Second draft"):
        draft = custody.admit(principal, str(uuid4()), "updates", text)
        custody.approve(draft["publication_id"], "U123", draft["digest"])
    calls = []
    def respond(request):
        calls.append(request.url.path)
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json={"ok": True, "team_id": "T123", "bot_id": "B123"})
        return httpx.Response(429, headers={"Retry-After": "60"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            worker = PublicationWorker(custody, client)
            await worker.submit_once()
            await worker.submit_once()
            assert await worker.submit_once() == 0
            restarted = PublicationStore(owner, custody.policy)
            restarted.bind_coordination("T123")
            assert await PublicationWorker(restarted, client).submit_once() == 0
    asyncio.run(run())
    assert calls == ["/api/auth.test", "/api/chat.postMessage"]


def test_one_installation_rate_limit_does_not_block_another(publication):
    import asyncio
    import json

    import httpx

    from src.agentbus_publication_runtime_service import PublicationWorker

    _, custody, principal, config = publication
    changed = copy.deepcopy(config)
    changed["installations"].append({"name": "other", "team_id": "T222", "bot_id": "B222", "token_env": "OTHER_TOKEN"})
    second = {**changed["destinations"][0], "name": "other-updates", "installation": "other", "channel": "C888"}
    changed["destinations"].append(second)
    custody.policy = PublicationPolicy(changed, channels=("C111", "C222"), operators=("U123",),
        environ={"CLIENT_TOKEN": "xoxb-private-fixture", "OTHER_TOKEN": "xoxb-other-fixture"})
    ids = []
    for destination in ("updates", "other-updates"):
        draft = custody.admit(principal, str(uuid4()), destination, "Isolated installation update")
        custody.approve(draft["publication_id"], "U123", draft["digest"])
        ids.append(draft["request_id"])
    posts = []
    def respond(request):
        other = request.headers["authorization"] == "Bearer xoxb-other-fixture"
        if request.url.path.endswith("auth.test"):
            return httpx.Response(200, json={"ok": True, "team_id": "T222" if other else "T123", "bot_id": "B222" if other else "B123"})
        body = json.loads(request.content)
        posts.append(body["channel"])
        if not other:
            return httpx.Response(429, headers={"Retry-After": "60"})
        return httpx.Response(200, json={"ok": True, "channel": "C888", "ts": "100.2", "message": {"text": body["text"]}})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            worker = PublicationWorker(custody, client)
            for _ in range(4):
                await worker.submit_once()
    asyncio.run(run())
    assert posts == ["C999", "C888"]
    assert custody.status(principal, ids[0])["state"] == "ready"
    assert custody.status(principal, ids[1])["state"] == "delivered"


def test_client_destination_cannot_overlap_coordination_or_switch_workspace(publication):
    _, custody, _, config = publication
    with pytest.raises(ValueError, match="workspace"):
        custody.bind_coordination("TOTHER")
    changed = copy.deepcopy(config)
    changed["destinations"][0]["channel"] = "C111"
    custody.policy = PublicationPolicy(changed, channels=("C111", "C222"), operators=("U123",),
                                       environ={"CLIENT_TOKEN": "xoxb-private-fixture"})
    with pytest.raises(ValueError, match="coordination"):
        custody.bind_coordination("T123")


@pytest.mark.parametrize("kind", ["slash_commands", "interactive", "events_api"])
@pytest.mark.parametrize("team", [None, "TOTHER"])
def test_foreign_workspace_socket_payloads_stop_before_lookup(publication_http, monkeypatch, kind, team):
    from slack_sdk.socket_mode.request import SocketModeRequest

    from src.agentbus_slack_socket_service import SlackReceiver

    _, app, _, _ = publication_http
    receiver = SlackReceiver(app.state.store, app.state.settings.channels, "B123", app.state.settings, workspace_id="T123")
    def deny(*args, **kwargs):
        raise AssertionError("Foreign workspace cannot reach target lookup or dispatch")
    monkeypatch.setattr(receiver._commands, "submit", deny)
    monkeypatch.setattr(app.state.store, "claim_slash_command", deny)
    payload = {"command": "/agentbus", "channel_id": "C111", "user_id": "U123", "text": "updates",
               "type": "block_actions", "user": {"id": "U123"}, "channel": {"id": "C111"},
               "container": {"type": "message", "channel_id": "C111"},
               "event": {"type": "message", "channel": "C111", "user": "U123", "ts": "100.1", "text": "Foreign"}}
    if team is not None:
        payload["team_id"] = team
        payload["team"] = {"id": team}
    before = app.state.store._db.execute("SELECT count(*) FROM messages").fetchone()[0]
    class Client:
        responses = []
        def send_socket_mode_response(self, response):
            self.responses.append(response)
    client = Client()
    receiver(client, SocketModeRequest(type=kind, envelope_id="foreign", payload=payload))
    receiver._commands.shutdown(wait=True)
    assert len(client.responses) == 1
    assert app.state.store._db.execute("SELECT count(*) FROM messages").fetchone()[0] == before


def test_private_approval_button_binds_digest_and_duplicate_has_no_new_effect(publication, tmp_path):
    import json

    from src.agentbus_settings_service import Settings
    from src.agentbus_slack_command_service import execute_command
    from src.agentbus_slack_ui_commands_service import execute_interaction

    owner, custody, principal, _ = publication
    owner.publications = custody
    settings = Settings(api_token="api-test", slack_bot_token="xoxb-fixture", slack_app_token="xapp-fixture",
        slack_channel="", slack_channels=("C111", "C222"), db_path=tmp_path / "unused.db",
        slack_operator_user_ids=frozenset({"U123", "U999"}), slack_command="/custom")
    draft = custody.admit(principal, str(uuid4()), "updates", "The exact approved client text")
    reply = execute_command({"command": "/custom", "user_id": "U123", "channel_id": "C111", "text": "updates"},
                             settings, owner, object())
    buttons = [button for block in reply.blocks if block["type"] == "actions" for button in block["elements"]]
    review = next(button for button in buttons if button["text"]["text"] == "Review exact update")
    opened = execute_interaction({"type": "block_actions", "team": {"id": "T123"}, "user": {"id": "U123"},
        "channel": {"id": "C111"}, "container": {"type": "message", "channel_id": "C111"}, "actions": [review]},
        settings, owner, object())
    buttons = [button for block in opened.blocks if block["type"] == "actions" for button in block["elements"]]
    approve = next(button for button in buttons if button["text"]["text"] == "Approve exact update")
    assert json.loads(approve["value"])["digest"] == draft["digest"]
    payload = {"type": "block_actions", "team": {"id": "T123"}, "user": {"id": "U123"},
               "channel": {"id": "C111"}, "container": {"type": "message", "channel_id": "C111"}, "actions": [approve]}
    tampered = copy.deepcopy(payload)
    value = json.loads(tampered["actions"][0]["value"])
    value["digest"] = "0" * 64
    tampered["actions"][0]["value"] = json.dumps(value)
    assert "does not match" in execute_interaction(tampered, settings, owner, object())
    assert custody.status(principal, draft["request_id"])["state"] == "awaiting_approval"
    denied = copy.deepcopy(payload)
    denied["user"]["id"] = "U999"
    assert "unavailable" in execute_interaction(denied, settings, owner, object())
    approved = execute_interaction(payload, settings, owner, object())
    assert approved.outcome == "committed" and approved.receipt == draft["publication_id"]
    row = custody.begin(draft["publication_id"])
    custody.settle(draft["publication_id"], "delivered", provider_ref="100.1")
    duplicate = execute_interaction(payload, settings, owner, object())
    assert "delivered" in duplicate.text and custody.begin(draft["publication_id"]) is None
    assert row["payload"] == custody._row(draft["publication_id"])["payload"]


def test_corrupted_retained_publication_payload_fails_before_post(publication):
    owner, custody, principal, _ = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "Immutable payload")
    custody.approve(draft["publication_id"], "U123", draft["digest"])
    owner._db.execute("UPDATE publication_requests SET payload=? WHERE request_id=?",
                      ('{"channel":"COTHER","client_msg_id":"altered"}', draft["request_id"]))
    owner._db.commit()
    assert custody.begin(draft["publication_id"]) is None
    assert custody.status(principal, draft["request_id"])["failure"] == "retained_payload_mismatch"


def test_same_request_uuid_in_different_channels_has_independent_publications(publication):
    owner, custody, principal, config = publication
    route, secret = "alpha:agentbus:other", "other-" + "z" * 40
    _, session, revision = owner.participation.enroll(repo="agentbus", route=route, display_name="other",
                                                   session_secret=secret, channel="C222")
    owner.participation.acknowledge_policy(session, secret, revision)
    other = owner.channels.principal(secret, route)
    changed = copy.deepcopy(config)
    changed["destinations"][0]["authors"].append(route)
    changed["destinations"][0]["source_channels"].append("C222")
    custody.policy = PublicationPolicy(changed, channels=("C111", "C222"), operators=("U123",),
                                       environ={"CLIENT_TOKEN": "xoxb-private-fixture"})
    request = str(uuid4())
    first = custody.admit(principal, request, "updates", "Own channel first")
    second = custody.admit(other, request, "updates", "Own channel second")
    assert first["request_id"] == second["request_id"] == request
    assert first["publication_id"] != second["publication_id"]
    assert custody.status(principal, request)["text"] == "Own channel first"
    assert custody.status(other, request)["text"] == "Own channel second"
    with pytest.raises(PermissionError):
        custody.operator_status(second["publication_id"], "U123", "C111")


def test_original_request_identity_is_part_of_approved_payload_digest(publication):
    owner, custody, principal, _ = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "Retain original identity")
    custody.approve(draft["publication_id"], "U123", draft["digest"])
    owner._db.execute("UPDATE publication_requests SET request_id=? WHERE publication_id=?",
                      (str(uuid4()), draft["publication_id"]))
    owner._db.commit()
    assert custody.begin(draft["publication_id"]) is None
    assert custody._row(draft["publication_id"])["failure"] == "retained_payload_mismatch"


def test_handoff_session_cannot_repost_prior_chat_publication_uuid(publication):
    from dataclasses import replace
    owner, custody, principal, _ = publication
    key = str(uuid4())
    old = custody.admit(principal, key, "updates", "Original session custody")
    replacement = str(uuid4())
    # An independently enrolled session has valid active authority, but this
    # original UUID remains reserved to the original session of the stable chat.
    owner._db.execute("INSERT INTO participation_sessions SELECT ?,chat_id,?,state,effective_policy_revision,delivered_policy_revision,acknowledged_policy_revision,joined_at,last_client_contact_at,last_semantic_ack_at,stopped_at,current_backoff_seconds,work_paused FROM participation_sessions WHERE session_id=?",
                      (replacement, "f" * 64, principal.session_id))
    owner._db.commit()
    new = replace(principal, session_id=replacement)
    with pytest.raises(PermissionError):
        custody.admit(new, key, "updates", "Original session custody")
    assert custody.status(principal, key)["publication_id"] == old["publication_id"]
    assert owner._db.execute("SELECT count(*) FROM publication_requests").fetchone()[0] == 1


def test_publication_preview_uses_exact_once_escaped_retained_blocks(publication):
    import json

    from src.agentbus_publication_ui_service import publication_card
    _, custody, principal, _ = publication
    draft = custody.admit(principal, str(uuid4()), "updates", "<@U999> & *bold*")
    payload = json.loads(custody._row(draft["publication_id"])["payload"])
    assert "&amp;amp;" not in json.dumps(payload)
    assert "&lt;@U999&gt; &amp; *bold*" in payload["blocks"][0]["text"]["text"]
    row = custody.operator_status(draft["publication_id"], "U123", "C111")
    blocks = publication_card(row, "C111")
    assert blocks[1:1 + len(payload["blocks"])] == payload["blocks"]


def test_many_long_publications_have_bounded_list_and_full_exact_review(publication):
    from src.agentbus_publication_ui_service import publication_card, publication_review
    _, custody, principal, _ = publication
    drafts = [custody.admit(principal, str(uuid4()), "updates", "\u202e" * 6000) for _ in range(5)]
    listing = publication_review(custody, "U123", "C111")
    assert len(listing.blocks) <= 50
    labels = [button["text"]["text"] for block in listing.blocks if block["type"] == "actions" for button in block["elements"]]
    assert "Approve exact update" not in labels and labels.count("Review exact update") == 5
    row = custody.operator_status(drafts[0]["publication_id"], "U123", "C111")
    full = publication_card(row, "C111")
    assert len(full) <= 50
    assert full[1:-1] == row["payload_blocks"]
