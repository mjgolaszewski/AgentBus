from __future__ import annotations

import json
import subprocess
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


@pytest.mark.parametrize("exit_code", [0, 1])
def test_runtime_probe_uses_retained_logs_after_exit(monkeypatch, capsys, exit_code):
    from scripts import run_project_gate as gate

    calls = []

    def docker(argv, *, capture=False):
        assert capture
        calls.append(argv)
        operation = argv[1]
        output, error = "", ""
        if operation == "wait":
            output = str(exit_code) + "\n"
        elif operation == "inspect":
            output = json.dumps({"Running": False, "Status": "exited", "ExitCode": exit_code})
        elif operation == "logs" and exit_code:
            error = "runtime-user-mismatch\n"
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=error)

    monkeypatch.setattr(gate, "run", docker)
    if exit_code:
        with pytest.raises(subprocess.CalledProcessError) as raised:
            gate._runtime_container_probe("qualified-image", "owned-container", "child-only-check")
        assert raised.value.stderr == "runtime-user-mismatch\n"
        assert capsys.readouterr().err == "runtime-user-mismatch\n"
    else:
        gate._runtime_container_probe("qualified-image", "owned-container", "child-only-check")
    assert [argv[1] for argv in calls] == ["create", "start", "wait", "inspect", "logs", "rm"]
    assert all("--attach" not in argv and "--force" not in argv for argv in calls)


@pytest.mark.parametrize("scenario", ["running", "logs_unavailable"])
def test_runtime_probe_retains_unconfirmed_container(monkeypatch, scenario):
    from scripts import run_project_gate as gate

    calls = []

    def docker(argv, *, capture=False):
        calls.append(argv)
        output = "1\n" if argv[1] == "wait" else ""
        if argv[1] == "inspect":
            output = json.dumps({"Running": scenario == "running",
                                 "Status": "running" if scenario == "running" else "exited",
                                 "ExitCode": 0 if scenario == "running" else 1})
        if argv[1] == "logs":
            return subprocess.CompletedProcess(argv, 7, stdout="", stderr="logs unavailable")
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    monkeypatch.setattr(gate, "run", docker)
    error = RuntimeError if scenario == "running" else subprocess.CalledProcessError
    with pytest.raises(error):
        gate._runtime_container_probe("qualified-image", "owned-container", "child-only-check")
    expected = ["create", "start", "wait", "inspect"]
    assert [argv[1] for argv in calls] == expected + ([] if scenario == "running" else ["logs"])
