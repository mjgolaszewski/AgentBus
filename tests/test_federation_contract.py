"""Issue #48 federation laws stay closed before implementation begins."""

import json
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "contracts/federation/v1/federation.contract.yml"
SCHEMA = ROOT / "schemas/federation-contract.schema.json"


def test_federation_contract_is_structurally_and_semantically_closed() -> None:
    contract = yaml.safe_load(CONTRACT.read_text())
    schema = json.loads(SCHEMA.read_text())
    Draft202012Validator(schema).validate(contract)

    invariant_ids = {item["id"] for item in contract["invariants"]}
    proof_ids = {item["id"] for item in contract["proof_obligations"]}
    unit_ids = {item["id"] for item in contract["implementation_order"]}
    assert len(invariant_ids) == len(contract["invariants"]) == 10
    assert len(proof_ids) == len(contract["proof_obligations"]) == 6
    assert len(unit_ids) == len(contract["implementation_order"]) == 5
    assert invariant_ids == {ref for proof in contract["proof_obligations"] for ref in proof["proves"]}
    assert "public_agentbus_endpoint" in contract["scope"]["non_goals"]
    assert contract["contracts"]["local_acceptance"]["provider_side_effect_after_commit"] is True
    assert contract["contracts"]["wake"]["admission"] == "owner_confirmed_idle_only"
    assert "remote_message_to_local_execution" in contract["contracts"]["prohibited_edges"]

    for unit in contract["implementation_order"]:
        assert set(unit["depends_on"]) <= unit_ids
    for gate in contract["acceptance_gates"].values():
        assert set(gate) <= invariant_ids | proof_ids
