"""Operator fan-out retains independent destination authority and shared custody."""

import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.agentbus_federation_bridge_service import FederationBridge
from src.agentbus_federation_wire_service import Binding, Identity, canonical
from src.agentbus_operator_federation_service import OperatorFederation
from src.agentbus_operator_scope_service import parse_scope
from src.agentbus_service import MessageStore
from src.agentbus_transaction_service import atomic


@pytest.fixture
def pair(tmp_path):
    identities = {n: Identity(n, n, "APP/BOT" + n + "/C111", Ed25519PrivateKey.generate()) for n in ("alpha", "beta")}
    stores = {n: MessageStore(tmp_path / (n + ".db")) for n in identities}
    bridges = {}
    for name, store in stores.items():
        store.channels.configure(("C111",))
        from src.agentbus_participation_service import DEFAULT_POLL_POLICY

        store.participation.set_policy(
            scope="global", scope_key="*", values=dict(DEFAULT_POLL_POLICY), actor="operator"
        )
        peer = "beta" if name == "alpha" else "alpha"
        other = identities[peer]
        binding = Binding(
            other.instance_id,
            other.route_prefix,
            other.publisher_id,
            other.private_key.public_key(),
            operator_version=4,
        )
        bridge = FederationBridge(store.federation, identities[name], {peer: binding}, object(), now=lambda: 1000.0)
        bridge.channel = "C111"
        policy = {
            "version": 1,
            "grants": [
                {
                    "origin": peer,
                    "actor": "U123",
                    "source_channel": "C111",
                    "channel": "C111",
                    "operations": ["health", "broadcast"],
                }
            ],
        }
        bridge.operator = OperatorFederation(store, bridge, policy)
        bridge.operator.health = lambda: {"version": "test", "slack_connected": True}
        bridges[name] = bridge
    yield stores, bridges, identities
    for store in stores.values():
        store.close()


def carriers(bridge, kind):
    return [
        dict(r)
        for r in bridge.store._db.execute("SELECT payload FROM bridge_obligations")
        if json.loads(r["payload"])["kind"] == kind
    ]


def deliver(source, destination, kind):
    for row in carriers(source, kind):
        destination.receive({"text": row["payload"], "publisher_id": source.identity.publisher_id, "ts": "100.1"})


def test_health_roundtrip_replay_and_revision_identity(pair):
    stores, bridges, _ = pair
    a, b = bridges["alpha"], bridges["beta"]
    key = a.operator.start(
        actor="U123", channel="C111", operation="health", location=None, text="", request_key="trigger"
    )
    assert (
        a.operator.start(
            actor="U123", channel="C111", operation="health", location=None, text="", request_key="trigger"
        )
        == key
    )
    with pytest.raises(ValueError, match="conflicts"):
        a.operator.start(
            actor="U123", channel="C111", operation="health", location="beta", text="", request_key="trigger"
        )
    deliver(a, b, "operator_request")
    deliver(a, b, "operator_request")
    deliver(b, a, "operator_result")
    rows = a.operator.results(key, "U123", "C111")
    assert [r["location"] for r in rows] == ["alpha", "beta"]
    assert all(r["state"] == "observed" for r in rows)
    assert len(carriers(b, "operator_result")) == 1
    with pytest.raises(ValueError):
        a.operator.results(key, "U999", "C111")
    with pytest.raises(ValueError):
        a.operator.results(key, "U123", "C222")
    frame = json.loads(carriers(a, "operator_request")[0]["payload"])
    frame["payload"] = "forged"
    with pytest.raises(ValueError):
        b.receive({"text": canonical(frame), "publisher_id": a.identity.publisher_id})


def enroll(store, name):
    secret = name + "x" * 40
    _, session, revision = store.participation.enroll(
        repo="agentbus", route="agentbus:" + name, display_name=name, session_secret=secret
    )
    store.participation.acknowledge_policy(session, secret, revision)
    return session, secret


def test_broadcast_snapshots_targets_once_and_reports_actual_read_receipts(pair):
    stores, bridges, _ = pair
    a, b = bridges["alpha"], bridges["beta"]
    session, secret = enroll(stores["beta"], "first")
    key = a.operator.start(
        actor="U123", channel="C111", operation="broadcast", location="beta", text="read me", request_key="broadcast"
    )
    deliver(a, b, "operator_request")
    enroll(stores["beta"], "late")
    deliver(a, b, "operator_request")
    row = stores["beta"]._db.execute("SELECT control_id FROM operator_remote_requests").fetchone()
    assert len(stores["beta"].controls.status(row["control_id"], channel="C111")) == 1
    stores["beta"].controls.deliver(session, secret)
    stores["beta"].controls.acknowledge_control(row["control_id"], session, secret)
    b.operator.refresh()
    deliver(b, a, "operator_result")
    result = a.operator.results(key, "U123", "C111")[0]
    assert result["targets"] == result["read_acknowledged"] == 1
    assert result["execution"] == "not_inferred"
    assert len(carriers(b, "operator_result")) == 2


def test_signed_but_ungranted_request_is_denied_and_cannot_issue_control(pair):
    stores, bridges, _ = pair
    a, b = bridges["alpha"], bridges["beta"]
    enroll(stores["beta"], "first")
    key = a.operator.start(
        actor="U999", channel="C111", operation="broadcast", location="beta", text="deny", request_key="denied"
    )
    deliver(a, b, "operator_request")
    deliver(b, a, "operator_result")
    assert a.operator.results(key, "U999", "C111")[0]["state"] == "denied"
    assert stores["beta"]._db.execute("SELECT count(*) FROM operator_controls").fetchone()[0] == 0


def test_shared_control_joins_caller_transaction_without_early_commit(pair):
    stores, _, _ = pair
    store = stores["alpha"]
    session, _ = enroll(store, "first")
    with pytest.raises(RuntimeError):
        with store._lock, atomic(store._db):
            store.controls.issue_auxiliary(
                kind="broadcast",
                routes=None,
                session_ids=[session],
                reason="rollback",
                actor="operator",
                request_key="rollback",
            )
            raise RuntimeError("caller rollback")
    assert store._db.execute("SELECT count(*) FROM operator_controls").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text,expected",
    [
        ("health --federated", ("health", None, "")),
        ("health --location beta", ("health", "beta", "")),
        ("broadcast --federated hello --literal", ("broadcast", None, "hello --literal")),
        ("broadcast --location beta -- --federated text", ("broadcast", "beta", "--federated text")),
        ("broadcast -- --federated text", ("local", None, "--federated text")),
        ("health", None),
        ("broadcast ordinary", None),
    ],
)
def test_scope_parser_preserves_literal_text_and_explicit_scope(text, expected):
    assert parse_scope(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "health --location",
        "health --federated extra",
        "health --location beta --federated",
        "broadcast --unknown hi",
        "broadcast --federated --location beta hello",
        "broadcast --federated",
    ],
)
def test_scope_parser_rejects_ambiguity_before_admission(text):
    with pytest.raises(ValueError):
        parse_scope(text)


@pytest.mark.parametrize("fault", ["revision", "hash", "nonfinite", "count"])
def test_operator_response_rejects_conflicting_or_invalid_results(pair, fault):
    from uuid import uuid4

    from src.agentbus_federation_wire_service import encode_frame

    _stores, bridges, _identities = pair
    a, b = bridges["alpha"], bridges["beta"]
    key = a.operator.start(
        actor="U123", channel="C111", operation="health", location="beta", text="", request_key="result-check"
    )
    deliver(a, b, "operator_request")
    deliver(b, a, "operator_result")
    before = a.operator.results(key, "U123", "C111")
    original = json.loads(carriers(b, "operator_result")[0]["payload"])
    body = json.loads(original["payload"])
    if fault == "revision":
        body["result"] = {"state": "denied"}
    if fault == "hash":
        body["request_hash"] = "0" * 64
    if fault == "nonfinite":
        body["sampled_at"] = float("nan")
    if fault == "count":
        body["result"] = {"state": "accepted", "targets": 1, "read_acknowledged": 2, "execution": "not_inferred"}
    hostile_payload = json.dumps(body, allow_nan=True) if fault == "nonfinite" else canonical(body)
    wire = encode_frame(b.identity, "operator_result", str(uuid4()), "alpha", hostile_payload, version=4)
    with pytest.raises(ValueError):
        a.receive({"text": wire, "publisher_id": b.identity.publisher_id, "ts": "100.2"})
    assert a.operator.results(key, "U123", "C111") == before


def test_expired_request_never_issues_a_control(pair):
    stores, bridges, _ = pair
    a, b = bridges["alpha"], bridges["beta"]
    enroll(stores["beta"], "first")
    key = a.operator.start(
        actor="U123", channel="C111", operation="broadcast", location="beta", text="late", request_key="expired"
    )
    a.now = b.now = lambda: 1701.0
    deliver(a, b, "operator_request")
    deliver(b, a, "operator_result")
    assert a.operator.results(key, "U123", "C111")[0]["state"] == "expired"
    assert stores["beta"]._db.execute("SELECT count(*) FROM operator_controls").fetchone()[0] == 0
