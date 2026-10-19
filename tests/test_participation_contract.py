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
    assert len(invariant_ids) == len(contract["invariants"]) == 41
    assert len(proof_ids) == len(contract["proof_obligations"])
    assert len(unit_ids) == len(contract["implementation_order"])
    assert {f"INV-{number:03}" for number in range(1, 42)} == invariant_ids
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


def test_current_authorization_model_resolves_inherited_rules_without_legacy_authority():
    """A v2 extension cannot silently inherit permission its channel law removes."""
    base = yaml.safe_load(CONTRACT.read_text())
    current = yaml.safe_load((ROOT / "contracts/participation/v2/participation.contract.yml").read_text())
    channel = yaml.safe_load((ROOT / current["channel_authority"]).read_text())
    assert ROOT / current["extends"] == CONTRACT
    assert base["document"]["status"] == "superseded"
    assert current["document"]["status"] == channel["document"]["status"] == "active"
    inherited = {entry["id"] for entry in base["invariants"]}
    replaced = current["inheritance"]["replaced_invariants"]
    assert set(replaced) <= inherited
    # Every inherited invariant explicitly permitting legacy or unrestricted
    # enrollment must have a named current replacement; unchanged controls stay.
    incompatible = {entry["id"] for entry in base["invariants"]
                    if any(term in entry["rule"] for term in (
                        "legacy messaging remains usable", "without per-chat operator intervention",
                        "old clients remain legible", "unjoined legacy", "unique across all sessions",
                        "Unjoined legacy callers"))}
    assert incompatible <= set(replaced)
    assert channel["authority"]["legacy_unjoined"] == "no message authority"
    assert channel["scope"]["immutable_channel_per_identity"] is True
    assert set(current["model"]["principal"]["fields"]) == {
        "chat_id", "session_id", "route", "channel", "inbox_id",
    }
    assert current["model"]["enrollment_grant"]["lifetime_seconds"] == channel["authority"]["grant_expiry_seconds"]
    assert current["experimental_boundary"]["atomic_idle_only_wake"] == "unqualified"


def test_primary_onboarding_discloses_operator_allowlist_and_native_limits():
    readme = (ROOT/'README.md').read_text()
    quick = readme[readme.index('## Quick start'):readme.index('## Quick start')+2900]
    assert quick.index('AGENTBUS_SLACK_OPERATOR_USER_IDS') < quick.index('./agentbus start')
    env = (ROOT/'.env.example').read_text()
    assert '\nAGENTBUS_SLACK_OPERATOR_USER_IDS=' in env
    assert 'Linux' in quick and '/proc' in quick and 'fcntl' in quick
    assert 'Native macOS and' in quick and 'Windows launchers are unsupported' in quick
    security = (ROOT/'SECURITY.md').read_text()
    assert 'same channel-scoped request authority and quota as `/agentbus send`' in security
    assert 'any human who can post' not in security
    model = (ROOT/'docs/contracts.md').read_text()
    assert 'single version authority' in ' '.join(model.split()) and 'RC5 is a corrective construction candidate' in model
    assert 'await their' not in model
    guide = (ROOT/'docs/README.md').read_text()
    assert 'The next RC3' not in guide
