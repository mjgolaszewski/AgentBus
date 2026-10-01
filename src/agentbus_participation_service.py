"""Deterministic policy decisions for AgentBus participation sessions.

Persistence and transport are separate owners; these rules take explicit inputs
so the same decision can be checked at join, policy change, and recovery.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Mapping

POLICY_FIELDS = frozenset({
    "initial_interval_seconds",
    "backoff_factor",
    "max_interval_seconds",
    "control_check_max_seconds",
    "jitter_fraction",
    "overdue_grace_seconds",
    "presentation_budget_bytes",
})


@dataclass(frozen=True)
class EffectivePolicy:
    values: dict[str, int | float | None]
    sources: dict[str, str]
    revision: str

    @property
    def initial_interval_seconds(self) -> float:
        return _positive(self.values, "initial_interval_seconds")

    @property
    def backoff_factor(self) -> float:
        return _positive(self.values, "backoff_factor")

    @property
    def max_interval_seconds(self) -> float:
        return _positive(self.values, "max_interval_seconds")

    @property
    def control_check_max_seconds(self) -> float:
        return _positive(self.values, "control_check_max_seconds")


def resolve_policy(
    global_revision: tuple[str, Mapping[str, int | float | None]],
    repo_revision: tuple[str, Mapping[str, int | float | None]] | None = None,
    chat_revision: tuple[str, Mapping[str, int | float | None]] | None = None,
    temporary_revision: tuple[str, Mapping[str, int | float | None]] | None = None,
) -> EffectivePolicy:
    """Resolve inheritance and one optional bounded session overlay."""
    values: dict[str, int | float | None] = {}
    sources: dict[str, str] = {}
    revisions: list[str] = []
    for scope, revision in (("global", global_revision), ("repo", repo_revision),
                            ("chat", chat_revision), ("temporary", temporary_revision)):
        if revision is None:
            continue
        revision_id, fields = revision
        unknown = set(fields) - POLICY_FIELDS
        if unknown:
            raise ValueError(f"unknown policy fields: {', '.join(sorted(unknown))}")
        if not revision_id:
            raise ValueError("policy revision identity is required")
        revisions.append(f"{scope}:{revision_id}")
        for field, value in fields.items():
            values[field] = value
            sources[field] = scope
    if set(values) != POLICY_FIELDS:
        raise ValueError("global policy must supply every policy field")
    initial = _positive(values, "initial_interval_seconds")
    factor = _positive(values, "backoff_factor")
    maximum = _positive(values, "max_interval_seconds")
    control_max = _positive(values, "control_check_max_seconds")
    if control_max < 1:
        raise ValueError("control check maximum must be at least one second")
    if factor < 1 or maximum < initial:
        raise ValueError("backoff factor must be at least 1 and maximum must cover initial interval")
    jitter = values["jitter_fraction"]
    grace = values["overdue_grace_seconds"]
    budget = values["presentation_budget_bytes"]
    if (not isinstance(jitter, (int, float)) or isinstance(jitter, bool)
            or not math.isfinite(jitter) or not 0 <= jitter < 1):
        raise ValueError("jitter fraction must be in [0, 1)")
    if (not isinstance(grace, (int, float)) or isinstance(grace, bool)
            or not math.isfinite(grace) or grace < 0):
        raise ValueError("overdue grace must be nonnegative")
    if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool) or budget < 1):
        raise ValueError("presentation budget must be a positive integer or null")
    digest_input = json.dumps(
        {"values": values, "sources": sources, "revisions": revisions},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    return EffectivePolicy(values, sources, hashlib.sha256(digest_input).hexdigest())


def _positive(values: Mapping[str, int | float | None], field: str) -> float:
    value = values[field]
    if (not isinstance(value, (int, float)) or isinstance(value, bool)
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{field} must be positive")
    return float(value)


def next_inbox_interval(
    policy: EffectivePolicy, previous_interval_seconds: float | None, *, semantic_delta: bool,
) -> float:
    """New semantic information resets backoff; unchanged reads do not."""
    if semantic_delta or previous_interval_seconds is None:
        return policy.initial_interval_seconds
    return min(policy.max_interval_seconds, previous_interval_seconds * policy.backoff_factor)


def next_control_check_interval(policy: EffectivePolicy, requested_interval_seconds: float) -> float:
    """A control probe's bound is independent of inbox backoff."""
    if requested_interval_seconds < 0:
        raise ValueError("requested interval must be nonnegative")
    return min(requested_interval_seconds, policy.control_check_max_seconds)
