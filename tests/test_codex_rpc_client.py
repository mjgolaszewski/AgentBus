"""Exercise the real JSON-line host protocol against a local inert fake."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.agentbus_codex_rpc_client import CodexAppServer, CodexHostRejected, TurnStartUncertain

FAKE_HOST = '''
import json, sys
mode = sys.argv[1]
for line in sys.stdin:
    item = json.loads(line)
    method = item.get("method")
    if method == "initialized":
        continue
    if method == "turn/start" and mode == "drop":
        sys.exit(0)
    if method == "turn/start" and mode == "reject":
        result = {"error": {"code": 409, "message": "thread is active"}}
    elif method == "thread/read":
        result = {"result": {"thread": {"id": item["params"]["threadId"],
            "status": {"type": "idle"}, "turns": [{"id": "turn-old", "status": "completed",
            "items": [{"type": "userMessage", "content": [{"type": "text",
            "text": "AGENTBUS_WAKE_ATTEMPT=attempt-old"}]}]}]}}}
    elif method == "thread/resume":
        if item["params"].get("excludeTurns") is not True:
            result = {"error": {"code": 413, "message": "full thread exceeds frame limit"}}
        else:
            result = {"result": {"thread": {"id": item["params"]["threadId"],
                "turns": []}}}
    elif method == "turn/start":
        result = {"result": {"turn": {"id": "turn-new", "status": "inProgress"}}}
    else:
        result = {"result": {}}
    print(json.dumps({"id": item["id"], **result}), flush=True)
'''


def host(tmp_path: Path, mode: str) -> CodexAppServer:
    script = tmp_path / "fake_host.py"
    script.write_text(FAKE_HOST)
    return CodexAppServer((sys.executable, str(script), mode), timeout=1)


def test_rpc_handshake_read_resume_start_and_history_reconciliation(tmp_path: Path):
    with host(tmp_path, "normal") as rpc:
        assert rpc.read_thread("thread-one")["status"]["type"] == "idle"
        rpc.resume("thread-one")
        assert rpc.start_turn("thread-one", "AGENTBUS_WAKE_ATTEMPT=attempt-new") == "turn-new"
        assert rpc.find_attempt("thread-one", "attempt-old") == ("turn-old", "completed")
        assert rpc.find_attempt("thread-one", "missing") is None


def test_explicit_rejection_is_distinct_from_uncertain_transport_loss(tmp_path: Path):
    with host(tmp_path, "reject") as rpc:
        with pytest.raises(CodexHostRejected, match="thread is active"):
            rpc.start_turn("thread-one", "wake")
    with host(tmp_path, "drop") as rpc:
        with pytest.raises(TurnStartUncertain):
            rpc.start_turn("thread-one", "wake")
