"""Security review mappings resolve to actual contracts and executable evidence."""

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_issue20_findings_have_closed_owner_and_evidence_mapping():
    mapping = yaml.safe_load((ROOT / "audits/p10-security-claim-mapping.yml").read_text())
    entries = mapping["entries"]
    assert len(entries) == 15
    assert {entry["finding"] for entry in entries} == {f"F{number}" for number in range(1, 16)}
    workitems = yaml.safe_load((ROOT / "plans/phase-10-workitems.yml").read_text())
    workitem_ids = {item["id"] for item in workitems["workitems"]}
    policy = yaml.safe_load((ROOT / "governance/evidence-policy.yml").read_text())
    for entry in entries:
        assert entry["workitem"] in workitem_ids
        assert entry["claim"] in policy["claim_dependencies"]
        assert entry["producer"] and entry["consumer"] and entry["owner"]
        contract_path, _, invariant = entry["contract"].partition("#")
        assert (ROOT / contract_path).is_file()
        if invariant:
            contract = yaml.safe_load((ROOT / contract_path).read_text())
            assert invariant in {item["id"] for item in contract["invariants"]}
        if entry["gate"] in {"test", "contract-test"}:
            module, node = entry["evidence"].split("::")
            source = ROOT / (module.replace(".", "/") + ".py")
            assert node in {item.name for item in ast.walk(ast.parse(source.read_text()))
                            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if entry["disposition"] in {
            "bounded_contract_with_disclosed_residual", "upstream_boundary_with_disclosed_residual",
        }:
            assert entry["residual"]


def test_mapping_preserves_explicit_security_exclusions():
    mapping = yaml.safe_load((ROOT / "audits/p10-security-claim-mapping.yml").read_text())
    by_id = {entry["finding"]: entry for entry in mapping["entries"]}
    assert all(by_id[finding].get("residual") for finding in ["F4", "F5", "F8", "F9", "F11", "F12", "F14"])
    assert not any(entry["disposition"] in {"closed", "verified", "accepted_risk"}
                   for entry in mapping["entries"])
