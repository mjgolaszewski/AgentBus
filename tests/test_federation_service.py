from __future__ import annotations

import sqlite3
import threading

import pytest

from src.agentbus_federation_service import FederationStore


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


def test_directory_revisions_are_monotonic_and_carry_no_authority():
    federation = store()
    current = federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                          revision=2, state="current", metadata={"repo": "agentbus"})
    stale = federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                       revision=1, state="withdrawn", metadata={})
    assert stale["revision"] == current["revision"] and stale["state"] == "current"
    with pytest.raises(ValueError, match="conflicting directory revision"):
        federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                   revision=2, state="withdrawn", metadata={})
    with pytest.raises(ValueError, match="authority"):
        federation.apply_directory(origin_instance_id="remote", route="agentbus:flower",
                                   revision=3, state="current", metadata={"credential": "nope"})
