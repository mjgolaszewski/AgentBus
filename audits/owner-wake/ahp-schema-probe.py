"""Validate pinned upstream wire shapes offline, without a host connection."""

import copy
import hashlib
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

SOURCE_COMMIT = "1ef0642965efaa7ef98caff287bca923fbc80ec6"
SCHEMA_SHA256 = "dcd9e6e2b2fd1693e7f403a3cf48ec416a8c4921ff9d377cfa37fab08fe94809"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python ahp-schema-probe.py SOURCE_ROOT")
    raw = (Path(sys.argv[1]) / "schema/actions.schema.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != SCHEMA_SHA256:
        raise SystemExit("upstream action schema pin mismatch")
    schema = json.loads(raw)
    validator = Draft202012Validator({
        "$ref": "#/$defs/ChatPendingMessageSetAction", "$defs": schema["$defs"],
    })
    base = {"type": "chat/pendingMessageSet", "kind": "queued", "id": "stable-attempt",
            "message": {"text": "AgentBus event references", "origin": {"kind": "user"}}}
    cases = []
    for name, change, expected in [
        ("queued", {}, True),
        ("steering_is_valid_protocol", {"kind": "steering"}, True),
        ("empty_attempt_id_is_schema_valid", {"id": ""}, True),
        ("non_user_origin_is_wire_valid",
         {"message": {"text": "references", "origin": {"kind": "agent"}}}, True),
        ("unknown_kind", {"kind": "idle_only"}, False),
        ("absent_kind", {"kind": None}, False),
        ("unrecognized_idle_precondition_is_not_capability", {"expectedIdleEpoch": 1}, True),
    ]:
        payload = copy.deepcopy(base)
        for key, value in change.items():
            if value is None:
                payload.pop(key)
            else:
                payload[key] = value
        accepted = validator.is_valid(payload)
        if accepted != expected:
            raise SystemExit(f"schema expectation failed: {name}")
        cases.append({"case": name, "schema_accepts": accepted})
    print(json.dumps({"source_commit": SOURCE_COMMIT, "schema_sha256": SCHEMA_SHA256,
                      "cases": cases, "scope": "wire_shape_only",
                      "host_connections": 0, "atomic_admission_proved": False}, indent=2))


if __name__ == "__main__":
    main()
