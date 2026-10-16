"""Atomic, revisioned directory recovery without remote authority promotion."""
import pytest
from test_federation_service import store


def entry(route="remote:a", role="original"):
    return {"route": route, "metadata": {"role": role}}


def snapshot(f, revision, entries):
    return f.directory_snapshot(origin_instance_id="remote", revision=revision, entries=entries)


def delta(f, revision, route="remote:a", role="changed"):
    return f.apply_directory_delta(origin_instance_id="remote", route=route,
        revision=revision, state="current", metadata={"role": role})


def test_malformed_snapshot_cannot_partially_change_existing_directory():
    f = store()
    snapshot(f, 1, [entry()])
    before = f.directory()
    with pytest.raises((ValueError, KeyError)):
        snapshot(f, 2, [entry(role="changed"), {}])
    assert f.directory() == before


def test_duplicate_snapshot_route_is_rejected_atomically():
    f = store()
    snapshot(f, 1, [entry()])
    before = f.directory()
    with pytest.raises(ValueError, match="duplicate"):
        snapshot(f, 2, [entry(role="changed"), entry(role="changed")])
    assert f.directory() == before


def test_complete_snapshot_withdraws_missing_route_only_for_its_origin():
    f = store()
    snapshot(f, 1, [entry(), entry("remote:b")])
    f.apply_directory(origin_instance_id="other", route="other:a", revision=1,
                      state="current", metadata={"role": "unrelated"})
    snapshot(f, 2, [entry()])
    rows = {r["route"]: r for r in f.directory()}
    assert rows["remote:b"]["state"] == "withdrawn"
    assert rows["other:a"]["state"] == "current"
    before = f.directory()
    snapshot(f, 1, [])
    assert f.directory() == before


def test_delta_revision_sequence_is_per_origin_across_routes():
    f = store()
    snapshot(f, 1, [entry(), entry("remote:b")])
    assert delta(f, 2)["state"] == "current"
    assert delta(f, 3, "remote:b")["state"] == "current"
    with pytest.raises(ValueError, match="conflicting"):
        delta(f, 3, role="revision-reused")


def test_gap_preserves_metadata_blocks_deltas_and_same_revision_snapshot_recovers():
    f = store()
    snapshot(f, 1, [entry(), entry("remote:b")])
    gap = delta(f, 4)
    assert gap["state"] == "gap" and gap["metadata"] == {"role": "original"}
    assert all(row["state"] == "gap" for row in f.directory())
    assert delta(f, 5, "remote:b")["state"] == "gap"
    snapshot(f, 5, [entry(role="recovered"), entry("remote:b", "new")])
    assert all(row["state"] == "current" for row in f.directory())
    assert delta(f, 6)["state"] == "current"


def test_conflicting_snapshot_or_invalid_delta_cannot_poison_valid_state():
    f = store()
    snapshot(f, 1, [entry()])
    before = f.directory()
    with pytest.raises(ValueError, match="conflicting"):
        snapshot(f, 1, [entry(role="forged")])
    with pytest.raises(ValueError):
        f.apply_directory_delta(origin_instance_id="remote", route="remote:a",
            revision=8, state="current", metadata={"credential": "secret"})
    assert f.directory() == before


def test_stale_directory_result_retains_decoded_metadata_schema():
    f = store()
    snapshot(f, 2, [entry()])
    stale = f.apply_directory(origin_instance_id="remote", route="remote:a",
        revision=1, state="current", metadata={"role": "stale"})
    assert stale == f.directory()[0]
    assert isinstance(stale["metadata"], dict)


def test_conflicting_late_snapshot_entry_rolls_back_earlier_writes():
    f = store()
    snapshot(f, 1, [entry(), entry("remote:b")])
    delta(f, 2, "remote:b", "accepted")
    before = f.directory()
    with pytest.raises(ValueError, match="conflicting"):
        snapshot(f, 2, [entry(role="would-change"), entry("remote:b", "conflict")])
    assert f.directory() == before
    assert delta(f, 3)["state"] == "current"


def test_revision_and_gap_recovery_survive_store_restart():
    from src.agentbus_federation_service import FederationStore

    f = store()
    snapshot(f, 1, [entry(), entry("remote:b")])
    delta(f, 4)
    before = f.directory()
    restarted = FederationStore(f._db, f._lock)
    snapshot(restarted, 3, [])
    assert restarted.directory() == before
    snapshot(restarted, 4, [entry(role="recovered")])
    assert restarted.directory()[0]["state"] == "current"
    assert restarted.directory()[1]["state"] == "withdrawn"
    assert delta(restarted, 5)["state"] == "current"


def test_snapshot_replay_is_idempotent_and_invalid_revision_cannot_withdraw():
    f = store()
    snapshot(f, 2, [entry()])
    before = f.directory()
    assert snapshot(f, 2, [entry()]) == before
    for invalid in (0, -1, True, "3"):
        with pytest.raises(ValueError):
            snapshot(f, invalid, [])
    assert f.directory() == before
