from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from scripts.run_project_gate import _runtime_smoke_image_tag


def test_runtime_smoke_tag_is_namespace_scoped() -> None:
    namespace = "session-123/workload-456/execution-789"
    tag = _runtime_smoke_image_tag(namespace)
    assert tag.startswith("agentbus:bcf-runtime-smoke-")
    assert tag == _runtime_smoke_image_tag(namespace)
    assert tag != _runtime_smoke_image_tag("session-123/workload-456/execution-other")


def test_runtime_smoke_tag_rejects_missing_namespace() -> None:
    with pytest.raises(ValueError, match="BCF_EXECUTION_STATE_NAMESPACE"):
        _runtime_smoke_image_tag(" ")


def test_concurrent_runtime_smoke_namespaces_do_not_collide() -> None:
    namespaces = [f"session-{index}/workload-{index}/execution-{index}" for index in range(32)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        tags = list(pool.map(_runtime_smoke_image_tag, namespaces))
    assert len(set(tags)) == len(namespaces)
