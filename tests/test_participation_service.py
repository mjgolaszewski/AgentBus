"""Policy behavior that must remain true through transport and persistence changes."""

import pytest

from src.agentbus_participation_service import (
    next_control_check_interval,
    next_inbox_interval,
    resolve_policy,
)

BASE_POLICY = {
    "initial_interval_seconds": 60,
    "backoff_factor": 2,
    "max_interval_seconds": 1920,
    "control_check_max_seconds": 60,
    "jitter_fraction": 0,
    "overdue_grace_seconds": 120,
    "presentation_budget_bytes": None,
}


def test_policy_resolution_is_revisioned_and_explains_field_provenance() -> None:
    policy = resolve_policy(
        ("global-1", BASE_POLICY),
        ("repo-2", {"max_interval_seconds": 480}),
        ("chat-3", {"max_interval_seconds": 240}),
    )
    assert policy.max_interval_seconds == 240
    assert policy.sources["max_interval_seconds"] == "chat"
    assert policy.sources["control_check_max_seconds"] == "global"
    assert policy.revision == resolve_policy(
        ("global-1", BASE_POLICY),
        ("repo-2", {"max_interval_seconds": 480}),
        ("chat-3", {"max_interval_seconds": 240}),
    ).revision
    assert policy.revision != resolve_policy(("global-1", BASE_POLICY), ("repo-2", {})).revision
    with pytest.raises(ValueError, match="unknown policy fields"):
        resolve_policy(("global-1", {**BASE_POLICY, "surprise": 1}))
    with pytest.raises(ValueError, match="must supply every"):
        resolve_policy(("global-1", {"initial_interval_seconds": 60}))


def test_backoff_caps_without_stopping_and_controls_keep_their_own_bound() -> None:
    policy = resolve_policy(("global-1", BASE_POLICY))
    intervals = []
    previous = None
    for _ in range(10):
        previous = next_inbox_interval(policy, previous, semantic_delta=False)
        intervals.append(previous)
    assert intervals == [60, 120, 240, 480, 960, 1920, 1920, 1920, 1920, 1920]
    assert next_control_check_interval(policy, previous) == 60
    assert next_inbox_interval(policy, previous, semantic_delta=True) == 60
    assert next_inbox_interval(policy, previous, semantic_delta=False) == 1920
    with pytest.raises(ValueError, match="nonnegative"):
        next_control_check_interval(policy, -1)


@pytest.mark.parametrize("field", [
    "initial_interval_seconds", "backoff_factor", "max_interval_seconds",
    "control_check_max_seconds", "jitter_fraction", "overdue_grace_seconds",
])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf")], ids=["nan", "infinity"])
def test_nonfinite_policy_values_reject_before_persistence(field, invalid) -> None:
    with pytest.raises(ValueError):
        resolve_policy(("global-1", {**BASE_POLICY, field: invalid}))
