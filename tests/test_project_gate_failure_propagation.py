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


def test_declared_contract_populations_match_actual_gate_producers() -> None:
    from pathlib import Path

    import yaml

    from scripts.run_project_gate import TEST_GATES

    root = Path(__file__).resolve().parents[1]
    registry = yaml.safe_load((root / "governance/gate-contracts.yml").read_text())
    for gate in ("contract-test", "python314-compatibility"):
        declared = registry["gates"][gate]["evidence"]["test_contract"]["selectors"]
        assert set(TEST_GATES[gate]) == set(declared), gate
