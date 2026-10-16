from __future__ import annotations

import sqlite3
import threading

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.agentbus_federation_service import FederationStore
from src.agentbus_federation_wire_service import Identity, encode_frame


def store() -> FederationStore:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    return FederationStore(db, threading.RLock())


def test_bridge_obligation_is_idempotent_and_requires_destination_acceptance():
    federation = store()
    first = federation.accept(origin_instance_id="home", origin_message_id="m1",
                              destination_instance_id="remote", payload="hello")
    again = federation.accept(origin_instance_id="home", origin_message_id="m1",
                              destination_instance_id="remote", payload="hello")
    assert first == again and first["state"] == "origin_accepted"
    assert first["payload"] == "hello"
    with pytest.raises(ValueError, match="conflicting federation payload"):
        federation.accept(origin_instance_id="home", origin_message_id="m1",
                          destination_instance_id="remote", payload="changed")
    with pytest.raises(ValueError, match="invalid bridge transition"):
        federation.transition("home", "m1", "remote", "acknowledged")
    federation.transition("home", "m1", "remote", "submitted", attempt=True)
    with pytest.raises(ValueError, match="invalid bridge transition"):
        federation.transition("home", "m1", "remote", "origin_accepted")
    federation.transition("home", "m1", "remote", "destination_accepted")
    assert federation.transition("home", "m1", "remote", "acknowledged")["state"] == "acknowledged"


def test_legacy_receipt_obligations_are_classified_during_schema_upgrade():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE bridge_obligations (
        origin_instance_id TEXT NOT NULL, origin_message_id TEXT NOT NULL,
        destination_instance_id TEXT NOT NULL, state TEXT NOT NULL,
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL, PRIMARY KEY(origin_instance_id, origin_message_id, destination_instance_id)
    )""")
    identity = Identity("remote", "remote", "remote-publisher", Ed25519PrivateKey.generate())
    signed_receipt = encode_frame(identity, "receipt", "receipt-1", "home", "{}")
    db.execute("INSERT INTO bridge_obligations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               ("remote", "receipt-1", "home", "submitted",
                signed_receipt, "hash", 1, "now"))
    db.execute("INSERT INTO bridge_obligations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               ("remote", "ordinary-1", "home", "submitted",
                '{"kind":"receipt","text":"ordinary legacy message payload"}', "hash", 1, "now"))
    db.execute("INSERT INTO bridge_obligations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
               ("remote", "opaque-1", "home", "submitted", "[]", "hash", 1, "now"))
    db.commit()
    federation = FederationStore(db, threading.RLock())
    row = federation.obligation("remote", "receipt-1", "home")
    assert row["kind"] == "receipt"
    assert federation.obligation("remote", "ordinary-1", "home")["kind"] == "message"
    assert federation.obligation("remote", "opaque-1", "home")["kind"] == "message"
    assert {item["origin_message_id"] for item in federation.pending(now=0, max_attempts=8)} == {
        "ordinary-1", "opaque-1"
    }

    # A later construction repeats the idempotent scan, covering a process
    # restart after the additive column was created but before classification.
    db.execute("UPDATE bridge_obligations SET kind='message' WHERE origin_message_id='receipt-1'")
    db.commit()
    FederationStore(db, threading.RLock())
    assert federation.obligation("remote", "receipt-1", "home")["kind"] == "receipt"


def test_legacy_receipt_migration_ignores_deep_or_incomplete_json():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE bridge_obligations (
        origin_instance_id TEXT NOT NULL, origin_message_id TEXT NOT NULL,
        destination_instance_id TEXT NOT NULL, state TEXT NOT NULL,
        payload TEXT NOT NULL, payload_hash TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL, PRIMARY KEY(origin_instance_id, origin_message_id, destination_instance_id)
    )""")
    deep = "[" * 15000 + "0" + "]" * 15000
    incomplete = '{"agentbus_federation":1,"kind":"receipt","text":"opaque"}'
    for message_id, payload in (("deep", deep), ("incomplete", incomplete)):
        db.execute("INSERT INTO bridge_obligations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   ("remote", message_id, "home", "submitted", payload, "hash", 1, "now"))
    db.commit()
    federation = FederationStore(db, threading.RLock())
    assert federation.obligation("remote", "deep", "home")["kind"] == "message"
    assert federation.obligation("remote", "incomplete", "home")["kind"] == "message"
    assert {item["origin_message_id"] for item in federation.pending(now=0, max_attempts=8)} == {
        "deep", "incomplete"
    }


def test_directory_revisions_are_monotonic_and_carry_no_authority():
    federation = store()
    current = federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                          revision=2, state="current", metadata={"role": "gardener"})
    stale = federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                       revision=1, state="withdrawn", metadata={})
    assert stale["revision"] == current["revision"] and stale["state"] == "current"
    with pytest.raises(ValueError, match="conflicting directory revision"):
        federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                   revision=2, state="withdrawn", metadata={})
    with pytest.raises(ValueError, match="authority"):
        federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                   revision=3, state="current", metadata={"credential": "nope"})


def test_origin_binding_and_directory_snapshot_are_operator_scoped():
    federation = store()
    federation.bind_origin(origin_instance_id="remote", publisher_id="slack-app-1")
    assert federation.verify_origin(origin_instance_id="remote", publisher_id="slack-app-1")
    assert not federation.verify_origin(origin_instance_id="remote", publisher_id="slack-app-2")
    with pytest.raises(ValueError, match="conflicting origin"):
        federation.bind_origin(origin_instance_id="remote", publisher_id="slack-app-2")

    entries = federation.directory_snapshot(
        origin_instance_id="remote", revision=4,
        entries=[
            {"route": "agentbus:flower", "metadata": {"role": "gardener"}},
            {"route": "agentbus:weed", "metadata": {"registration_state": "active"}},
        ],
    )
    assert len(entries) == 2
    assert [item["route"] for item in federation.directory()] == [
        "agentbus:flower", "agentbus:weed"
    ]


def test_payload_and_directory_bounds_fail_closed():
    federation = store()
    with pytest.raises(ValueError, match="payload"):
        federation.accept(origin_instance_id="home", origin_message_id="m1",
                          destination_instance_id="remote", payload=b"x" * 48_001)
    with pytest.raises(ValueError, match="unapproved"):
        federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                   revision=1, state="current", metadata={"repo": "secret"})
    with pytest.raises(ValueError, match="snapshot"):
        federation.directory_snapshot(origin_instance_id="remote", revision=1,
                                      entries=[{}] * 257)


def test_registration_projection_is_service_owned_and_non_authoritative():
    federation = store()
    assert federation.publish_registration(
        origin_instance_id="home", route="home:gardener", revision=1,
        registration_state="joining", metadata={"agent_id": "gardener"}
    ) is None
    active = federation.publish_registration(
        origin_instance_id="home", route="home:gardener", revision=2,
        registration_state="active",
        metadata={"agent_id": "gardener", "role": "signal"},
    )
    assert active["state"] == "current"
    assert active["metadata"]["registration_state"] == "active"
    withdrawn = federation.publish_registration(
        origin_instance_id="home", route="home:gardener", revision=3,
        registration_state="stopped", metadata={},
    )
    assert withdrawn["state"] == "withdrawn"


def test_destination_acceptance_requires_bound_origin_and_deduplicates():
    federation = store()
    federation.bind_origin(origin_instance_id="remote", publisher_id="slack-app-1")
    first = federation.accept_destination(
        origin_instance_id="remote", publisher_id="slack-app-1",
        origin_message_id="m1", destination_instance_id="home", payload="hello",
    )
    again = federation.accept_destination(
        origin_instance_id="remote", publisher_id="slack-app-1",
        origin_message_id="m1", destination_instance_id="home", payload="hello",
    )
    assert first == again
    with pytest.raises(ValueError, match="untrusted"):
        federation.accept_destination(
            origin_instance_id="remote", publisher_id="forged",
            origin_message_id="m2", destination_instance_id="home", payload="hello",
        )
    with pytest.raises(ValueError, match="conflicting"):
        federation.accept_destination(
            origin_instance_id="remote", publisher_id="slack-app-1",
            origin_message_id="m1", destination_instance_id="home", payload="changed",
        )


def test_directory_delta_gap_is_explicit_and_requires_resync():
    federation = store()
    federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                revision=1, state="current", metadata={"role": "gardener"})
    gap = federation.apply_directory_delta(
        origin_instance_id="remote", route="agentbus:flower", revision=4,
        state="current", metadata={"role": "changed"},
    )
    assert gap["state"] == "gap"
    assert gap["revision"] == 4
