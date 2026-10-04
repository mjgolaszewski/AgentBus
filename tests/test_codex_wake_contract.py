"""Issue #8's normative owner is structurally and semantically closed."""

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def test_codex_wake_contract_is_closed() -> None:
    contract = yaml.safe_load((ROOT / "contracts/codex-wake/v1/codex-wake.contract.yml").read_text())
    schema = json.loads((ROOT / "schemas/codex-wake-contract.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(contract)
    invariants = {item["id"] for item in contract["invariants"]}
    proofs = {item["id"] for item in contract["proof_obligations"]}
    units = {item["id"] for item in contract["implementation_order"]}
    assert len(invariants) == len(contract["invariants"]) == 22
    assert len(proofs) == len(contract["proof_obligations"]) == 19
    assert len(units) == len(contract["implementation_order"]) == 9
    assert invariants == {ref for proof in contract["proof_obligations"] for ref in proof["proves"]}
    for refs in contract["acceptance_gates"].values():
        assert refs and set(refs) <= invariants | proofs | units
    pending = {item["id"]: set(item["depends_on"]) for item in contract["implementation_order"]}
    assert all(deps <= units for deps in pending.values())
    complete: set[str] = set()
    while pending:
        ready = {unit for unit, deps in pending.items() if deps <= complete}
        assert ready, "implementation cycle"
        complete.update(ready)
        for unit in ready:
            del pending[unit]
    assert contract["contracts"]["wake"]["dry_run"] == "default"
    assert contract["architecture_constraints"]["activation"] == "workspace_disabled_by_default_and_live_requires_workspace_enablement"
    assert "auto_enrollment" in contract["contracts"]["bind"]
    assert contract["contracts"]["wake"]["rate_policy"]["max_followups_for_unchanged_candidates"] == 1
    assert "stopped_no_auto_wake" in contract["contracts"]["lifecycle"]["states"]
    assert contract["contracts"]["wake"]["uncertain_turn_start"] == "bounded_paginated_recent_turn_reconciliation_or_halt_for_operator_never_full_history_or_blind_retry"
    assert {"thread/turns/list", "thread/items/list", "turn/interrupt"} <= set(contract["primitives"]["host_capability"]["allowed_methods"])
    assert "TURN_COMPLETION_UNCERTAIN" in contract["failures"]
    assert contract["primitives"]["experimental_proxy"]["mode"] == "experimental_vs_code_proxy"
    assert contract["primitives"]["experimental_proxy"]["fallback_to_competing_stdio"] == "forbidden"
    assert contract["contracts"]["storage"]["direct_service_database_access"] == "prohibited"
    assert contract["contracts"]["wake_scan"]["advancement"] == "only_after_candidate_ids_are_durable"
