from __future__ import annotations

import subprocess

import pytest

from scripts.run_project_gate import require_success


def test_failed_child_cannot_be_reported_as_success() -> None:
    failed = subprocess.CompletedProcess(
        ["docker", "run"], returncode=17, stdout="", stderr="runtime-user-mismatch"
    )
    with pytest.raises(subprocess.CalledProcessError) as raised:
        require_success(failed)
    assert raised.value.returncode == 17
    assert raised.value.stderr == "runtime-user-mismatch"
