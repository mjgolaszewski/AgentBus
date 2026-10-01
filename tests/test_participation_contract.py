"""Keep Issue #6's normative contract closed before behavior is implemented."""

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "contracts/participation/v1/participation.contract.yml"
SCHEMA = ROOT / "schemas/participation-contract.schema.json"


def test_participation_contract_is_structurally_and_semantically_closed() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(contract)

    invariant_ids = {item["id"] for item in contract["invariants"]}
    proof_ids = {item["id"] for item in contract["proof_obligations"]}
    unit_ids = {item["id"] for item in contract["implementation_order"]}
    assert len(invariant_ids) == len(contract["invariants"]) == 19
    assert len(proof_ids) == len(contract["proof_obligations"])
    assert len(unit_ids) == len(contract["implementation_order"])
    assert {f"INV-{number:03}" for number in range(1, 20)} == invariant_ids
    assert invariant_ids == {
        invariant
        for proof in contract["proof_obligations"]
        for invariant in proof["proves"]
    }

    for gate_refs in contract["acceptance_gates"].values():
        assert gate_refs
        assert set(gate_refs) <= invariant_ids | proof_ids

    pending = {item["id"]: set(item["depends_on"]) for item in contract["implementation_order"]}
    assert all(deps <= unit_ids for deps in pending.values())
    completed: set[str] = set()
    while pending:
        ready = {unit for unit, deps in pending.items() if deps <= completed}
        assert ready, "implementation dependency cycle"
        completed.update(ready)
        for unit in ready:
            del pending[unit]

    assert contract["contracts"]["authority"]["sender_label"] == "never_authority"
    assert contract["contracts"]["authority"]["agent_ack"] == (
        "session_specific_proof_of_possession_bound_to_exact_chat_and_session"
    )
    assert contract["contracts"]["stop"]["silence"] == "never_success"
    assert contract["contracts"]["stop"]["sequence"] == [
        "durable_issue", "delivery", "explicit_target_ack", "durable_ack_commit",
        "receipt_return", "polling_ceases", "external_agent_ends_turn",
    ]
    assert contract["contracts"]["poll"]["empty_unchanged_cycle"] == "zero_model_visible_output"
    assert contract["contracts"]["poll"]["semantic_return"] == (
        "worker_continues_polling_after_model_visible_event"
    )
    assert contract["primitives"]["participation_session"]["states"] == [
        "joining", "active", "overdue", "stopping", "stopped",
    ]
