"""Versioned opt-in directory exchange; fake fan-out is not provider proof."""
import asyncio
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.agentbus_federation_bridge_service import FederationBridge
from src.agentbus_federation_directory_service import DirectorySynchronizer
from src.agentbus_federation_wire_service import Binding, Identity, decode_frame, encode_frame
from src.agentbus_service import MessageStore


class Roster:
    def __init__(self, route, chat):
        self.rows = [{"chat_id": chat, "route": route, "state": "joining",
                      "last_client_contact_at": "2026-10-16T00:00:00Z",
                      "session_secret": "SECRET", "private_path": "/private/secret"}]

    def roster(self, *, include_stopped=False):
        return self.rows


class SharedProvider:
    def __init__(self, events, publisher):
        self.events, self.publisher = events, publisher

    async def publish(self, text):
        stamp = f"{len(self.events)+1}.000001"
        self.events.append({"publisher_id": self.publisher, "text": text, "ts": stamp})
        return stamp

    async def history(self, after, limit):
        return [e for e in self.events if float(e["ts"]) > float(after)][:limit]


def test_directory_v2_requires_peer_opt_in_and_preserves_v1_message_receipt():
    key = Ed25519PrivateKey.generate()
    identity = Identity("home", "home", "publisher", key)
    old = Binding("home", "home", "publisher", key.public_key())
    for kind in ("message", "receipt"):
        text = encode_frame(identity, kind, "m1", "remote", "{}")
        assert decode_frame(text, "publisher", "remote", {"home": old})["agentbus_federation"] == 1
    directory = encode_frame(identity, "directory", "m1", "remote", "{}")
    with pytest.raises(ValueError):
        decode_frame(directory, "publisher", "remote", {"home": old})
    approved = Binding("home", "home", "publisher", key.public_key(), directory_version=2)
    assert decode_frame(directory, "publisher", "remote", {"home": approved})["agentbus_federation"] == 2


def test_three_fake_installations_late_join_rename_withdraw_without_authority(tmp_path):
    names = ("home", "remote", "late")
    keys = {n: Ed25519PrivateKey.generate() for n in names}
    bindings = {n: Binding(n, n, n+"-publisher", keys[n].public_key(), directory_version=2) for n in names}
    events, stores, bridges, rosters = [], {}, {}, {}
    try:
        for index, name in enumerate(names):
            stores[name] = MessageStore(tmp_path / (name+".sqlite3"))
            chat = f"00000000-0000-4000-8000-{index:012d}"
            rosters[name] = Roster(name+":agentbus:flower", chat)
            bridges[name] = FederationBridge(stores[name].federation,
                Identity(name, name, name+"-publisher", keys[name]), bindings,
                SharedProvider(events, name+"-publisher"), now=lambda: 10000.0)
            bridges[name].directory = DirectorySynchronizer(bridges[name], rosters[name],
                share_chat_ids=[chat], peers=[n for n in names if n != name])

        def settle(active):
            for _ in range(6):
                for name in active:
                    bridges[name].directory.reconcile()
                    asyncio.run(bridges[name].submit_once())
                for name in active:
                    asyncio.run(bridges[name].recover_once(limit=1000))

        settle(("home", "remote"))
        assert not stores["remote"].federation.directory(origin_instance_id="home")
        rosters["home"].rows[0]["state"] = "active_compliant"
        settle(("home", "remote"))
        assert stores["remote"].federation.directory(origin_instance_id="home")[0]["state"] == "current"
        settle(names)
        assert stores["late"].federation.directory(origin_instance_id="home")[0]["state"] == "current"
        rosters["home"].rows[0]["route"] = "home:agentbus:renamed"
        settle(names)
        rows = {r["route"]:r for r in stores["late"].federation.directory(origin_instance_id="home")}
        assert rows["home:agentbus:flower"]["state"] == "withdrawn"
        assert rows["home:agentbus:renamed"]["metadata"]["agent_id"] == rosters["home"].rows[0]["chat_id"]
        rosters["home"].rows[0]["state"] = "stopped"
        settle(names)
        assert all(r["state"] == "withdrawn" for r in stores["late"].federation.directory(origin_instance_id="home"))
        before = len(events)
        settle(names)
        assert len(events) == before
        assert "SECRET" not in json.dumps(events) and "/private/secret" not in json.dumps(events)
        for store in stores.values():
            assert store._db.execute("SELECT COUNT(*) FROM federated_messages").fetchone()[0] == 0
            assert store._db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
            assert store.participation.roster() == []
    finally:
        for store in stores.values():
            store.close()


def test_directory_payload_rejects_foreign_namespace_and_noncanonical_bytes():
    from src.agentbus_federation_directory_service import decode_directory
    from src.agentbus_federation_wire_service import canonical

    body = {"directory": 1, "operation": "snapshot", "revision": 1,
            "entries": [{"route": "home:agentbus:forged", "metadata": {}}]}
    with pytest.raises(ValueError, match="origin binding"):
        decode_directory(canonical(body), "remote")
    body["entries"] = []
    with pytest.raises(ValueError, match="not canonical"):
        decode_directory(json.dumps(body), "remote")
    body["session_secret"] = "SECRET"
    with pytest.raises(ValueError):
        decode_directory(canonical(body), "remote")


@pytest.mark.parametrize('version', [True, 1.0])
def test_directory_version_rejects_numeric_aliases(version):
    from src.agentbus_federation_directory_service import decode_directory
    from src.agentbus_federation_wire_service import canonical

    with pytest.raises(ValueError):
        decode_directory(canonical({"directory": version, "operation": "snapshot",
                                    "revision": 1, "entries": []}), "remote")


def receiver(tmp_path):
    local, remote = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    store = MessageStore(tmp_path / "receiver.sqlite3")
    peer = Identity("remote", "remote", "remote-publisher", remote)
    bridge = FederationBridge(store.federation, Identity("home", "home", "home-publisher", local),
        {"remote": Binding("remote", "remote", "remote-publisher", remote.public_key(), directory_version=2)},
        SharedProvider([], "home-publisher"), now=lambda: 10000.0)
    roster = Roster("home:agentbus:flower", "00000000-0000-4000-8000-000000000001")
    bridge.directory = DirectorySynchronizer(bridge, roster, share_chat_ids=[], peers=["remote"])
    return bridge, store, peer


def directory_event(peer, payload, message_id=None):
    import hashlib

    from src.agentbus_federation_wire_service import canonical

    text = canonical(payload)
    message_id = message_id or "directory-" + hashlib.sha256(text.encode()).hexdigest()
    return {"publisher_id": peer.publisher_id, "text": encode_frame(peer, "directory", message_id, "home", text)}


def test_directory_rejects_wrong_content_identity_before_custody(tmp_path):
    bridge, store, peer = receiver(tmp_path)
    try:
        event = directory_event(peer, {"directory": 1, "operation": "snapshot",
            "revision": 1, "entries": []}, "directory-" + "0" * 64)
        with pytest.raises(ValueError, match="canonical payload bytes"):
            bridge.receive(event)
        assert store.federation.directory() == []
        assert store._db.execute("SELECT COUNT(*) FROM bridge_obligations").fetchone()[0] == 0
    finally:
        store.close()


def test_directory_request_retry_restart_and_rate_do_not_create_storms(tmp_path):
    bridge, store, peer = receiver(tmp_path)
    try:
        request = {"directory": 1, "operation": "request", "after_revision": 0,
                   "request_id": "00000000-0000-4000-8000-000000000001"}
        event = directory_event(peer, request)
        bridge.receive(event)
        before = store._db.total_changes
        bridge.receive(event)
        bridge.directory = DirectorySynchronizer(bridge, Roster("home:agentbus:flower",
            "00000000-0000-4000-8000-000000000001"), share_chat_ids=[], peers=["remote"])
        bridge.receive(event)
        assert store._db.total_changes == before
        request["request_id"] = "00000000-0000-4000-8000-000000000002"
        with pytest.raises(ValueError, match="rate exceeded"):
            bridge.receive(directory_event(peer, request))
        assert store._db.total_changes == before
        assert store._db.execute("SELECT COUNT(*) FROM bridge_obligations").fetchone()[0] == 2
    finally:
        store.close()


def test_outgoing_resync_request_is_coalesced_across_service_restart(tmp_path):
    bridge, store, _ = receiver(tmp_path)
    try:
        bridge.directory.reconcile()
        before = store._db.total_changes
        obligations = store._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0]
        for _ in range(3):
            bridge.directory = DirectorySynchronizer(bridge, Roster('home:agentbus:flower',
                '00000000-0000-4000-8000-000000000001'), share_chat_ids=[], peers=['remote'])
            bridge.directory.reconcile()
        assert store._db.total_changes == before
        assert store._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == obligations == 2
    finally:
        store.close()


def test_signed_directory_revision_overflow_is_rejected_before_persistence(tmp_path):
    bridge, store, peer = receiver(tmp_path)
    try:
        event = directory_event(peer, {"directory": 1, "operation": "snapshot",
            "revision": 2 ** 100, "entries": []})
        before = store._db.total_changes
        with pytest.raises(ValueError):
            bridge.receive(event)
        assert store._db.total_changes == before
        assert store.federation.directory_revision('remote')['revision'] == 0
    finally:
        store.close()


def test_signed_gap_requests_resync_and_conflicting_revision_has_no_receipt(tmp_path):
    bridge, store, peer = receiver(tmp_path)
    try:
        snapshot = {"directory": 1, "operation": "snapshot", "revision": 1,
                    "entries": [{"route": "remote:agentbus:flower", "metadata": {"role": "old"}}]}
        bridge.receive(directory_event(peer, snapshot))
        delta = {"directory": 1, "operation": "delta", "revision": 3,
                 "route": "remote:agentbus:flower", "state": "current", "metadata": {"role": "new"}}
        bridge.receive(directory_event(peer, delta))
        row = store.federation.directory()[0]
        assert row["state"] == "gap" and row["metadata"] == {"role": "old"}
        bridge.directory.reconcile()
        requests = [json.loads(json.loads(r["payload"])["payload"]) for r in
                    store._db.execute("SELECT payload FROM bridge_obligations WHERE kind='directory'")]
        assert any(r["operation"] == "request" and r["after_revision"] == 1 for r in requests)
        snapshot["revision"] = 3
        snapshot["entries"][0]["metadata"] = {"role": "new"}
        bridge.receive(directory_event(peer, snapshot))
        assert store.federation.directory()[0]["state"] == "current"
        before = store._db.total_changes
        snapshot["entries"][0]["metadata"] = {"role": "conflict"}
        with pytest.raises(ValueError, match="conflicting"):
            bridge.receive(directory_event(peer, snapshot))
        assert store._db.total_changes == before
        assert store.federation.directory()[0]["metadata"] == {"role": "new"}
    finally:
        store.close()


def test_directory_v2_still_requires_signed_publisher_and_exact_destination(tmp_path):
    bridge, store, peer = receiver(tmp_path)
    try:
        event = directory_event(peer, {"directory": 1, "operation": "snapshot", "revision": 1, "entries": []})
        wrong_publisher = dict(event, publisher_id="attacker-publisher")
        frame = json.loads(event["text"])
        frame["destination_instance_id"] = "elsewhere"
        wrong_destination = dict(event, text=json.dumps(frame))
        frame = json.loads(event["text"])
        frame["signature"] = "A" * len(frame["signature"])
        wrong_signature = dict(event, text=json.dumps(frame))
        for hostile in (wrong_publisher, wrong_destination, wrong_signature):
            with pytest.raises(ValueError):
                bridge.receive(hostile)
        assert store._db.execute("SELECT COUNT(*) FROM bridge_obligations").fetchone()[0] == 0
        assert store.federation.directory_revision("remote")["revision"] == 0
    finally:
        store.close()
