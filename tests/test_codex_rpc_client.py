"""Exercise the real JSON-line host protocol against a local inert fake."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from src.agentbus_codex_rpc_client import CodexAppServer, CodexHostRejected, TurnStartUncertain

FAKE_HOST = '''
import json, pathlib, sys, time
mode = sys.argv[1]
calls = pathlib.Path(sys.argv[2])
approval_rejected = False
for line in sys.stdin:
    item = json.loads(line)
    method = item.get("method")
    if mode in {"approval", "approval-early", "approval-other", "approval-grace"} and item.get("id") == 99 and method is None:
        approval_rejected = isinstance(item.get("error"), dict)
        with calls.open("a") as stream:
            stream.write(json.dumps({"method": "approval-response", "error": item.get("error")}) + "\\n")
        continue
    if method == "initialized":
        continue
    with calls.open("a") as stream:
        stream.write(json.dumps({"method": method, "params": item.get("params")}) + "\\n")
    if method == "turn/start" and mode == "drop":
        sys.exit(0)
    if method == "turn/start" and mode == "reject":
        result = {"error": {"code": 409, "message": "thread is active"}}
    elif method == "thread/read":
        if item["params"].get("includeTurns") is True:
            result = {"error": {"code": 413, "message": "full thread exceeds frame limit"}}
        else:
            result = {"result": {"thread": {"id": item["params"]["threadId"],
                "status": {"type": "idle"}, "turns": []}}}
    elif method == "thread/turns/list":
        params = item["params"]
        if (params.get("sortDirection") != "desc" or params.get("itemsView") != "notLoaded"
                or not isinstance(params.get("limit"), int) or params["limit"] > 100):
            result = {"error": {"code": 413, "message": "turn page must be bounded metadata"}}
        else:
            result = {"result": {"data": [
                {"id": "turn-new", "status": "completed", "itemsView": "notLoaded", "items": []},
                {"id": "turn-old", "status": "completed", "itemsView": "notLoaded", "items": []}],
                "nextCursor": None, "backwardsCursor": "older"}}
    elif method == "thread/items/list":
        params = item["params"]
        if (params.get("sortDirection") != "asc" or not params.get("turnId")
                or not isinstance(params.get("limit"), int) or params["limit"] > 100):
            result = {"error": {"code": 413, "message": "item page must be bounded by turn"}}
        else:
            marker = "AGENTBUS_WAKE_ATTEMPT=other"
            if params["turnId"] == "turn-old" and mode != "missing":
                marker = "AGENTBUS_WAKE_ATTEMPT=attempt-old"
            result = {"result": {"data": [{"turnId": params["turnId"], "item": {
                "type": "userMessage", "content": [{"type": "text", "text": marker}]},
                "startedAtMs": None, "completedAtMs": None}],
                "nextCursor": None, "backwardsCursor": "earlier"}}
    elif method == "thread/resume":
        if item["params"].get("excludeTurns") is not True:
            result = {"error": {"code": 413, "message": "full thread exceeds frame limit"}}
        else:
            result = {"result": {"thread": {"id": item["params"]["threadId"],
                "turns": []}}}
    elif method == "turn/start":
        result = {"result": {"turn": {"id": "turn-new", "status": "inProgress"}}}
    elif method == "turn/interrupt":
        result = ({"error": {"code": 409, "message": "turn already terminal"}}
                  if mode == "terminal-before-error" else {"result": {}})
    else:
        result = {"result": {}}
    if method == "turn/start" and mode == "early":
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-new", "status": "completed"}}}), flush=True)
    if method == "turn/start" and mode in {"approval-early", "approval-other"}:
        requested_turn = "turn-other" if mode == "approval-other" else "turn-new"
        print(json.dumps({"id": 99, "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-one", "turnId": requested_turn}}), flush=True)
    if method == "turn/interrupt" and mode == "terminal-before-error":
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-new", "status": "interrupted"}}}), flush=True)
    print(json.dumps({"id": item["id"], **result}), flush=True)
    if method == "turn/start" and mode == "approval":
        print(json.dumps({"id": 99, "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-one", "turnId": "turn-new"}}), flush=True)
    if method == "turn/start" and mode == "approval-grace":
        time.sleep(0.04)
        print(json.dumps({"id": 99, "method": "item/commandExecution/requestApproval",
            "params": {"threadId": "thread-one", "turnId": "turn-new"}}), flush=True)
    if method == "turn/start" and mode == "malformed-after-accept":
        print("{not-json", flush=True)
    if method == "turn/start" and mode == "delayed":
        time.sleep(0.2)
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-new", "status": "completed"}}}), flush=True)
    if method == "turn/interrupt" and mode == "stall":
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-new", "status": "interrupted"}}}), flush=True)
    if method == "turn/interrupt" and mode == "malformed-after-accept":
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-new", "status": "interrupted"}}}), flush=True)
    if method == "turn/interrupt" and mode in {"approval", "approval-early"} and approval_rejected:
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-new", "status": "interrupted"}}}), flush=True)
    if method == "turn/interrupt" and mode == "approval-grace" and approval_rejected:
        time.sleep(0.12)
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-new", "status": "interrupted"}}}), flush=True)
    if method == "turn/start" and mode == "approval-other":
        response = json.loads(sys.stdin.readline())
        with calls.open("a") as stream:
            stream.write(json.dumps({"method": "approval-response",
                "error": response.get("error")}) + "\\n")
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-new", "status": "completed"}}}), flush=True)
    if method == "turn/start" and mode == "failed":
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-new", "status": "failed"}}}), flush=True)
'''


def host(tmp_path: Path, mode: str) -> CodexAppServer:
    script = tmp_path / "fake_host.py"
    script.write_text(FAKE_HOST)
    return CodexAppServer((sys.executable, str(script), mode,
                           str(tmp_path / "requests.jsonl")), timeout=1)


def requests(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]


def test_rpc_handshake_read_resume_start_and_history_reconciliation(tmp_path: Path):
    with host(tmp_path, "normal") as rpc:
        assert rpc.read_thread("thread-one")["status"]["type"] == "idle"
        rpc.resume("thread-one")
        assert rpc.start_turn("thread-one", "AGENTBUS_WAKE_ATTEMPT=attempt-new") == "turn-new"
        assert rpc.find_attempt("thread-one", "attempt-old") == ("turn-old", "completed")
        assert rpc.find_attempt("thread-one", "missing") is None


def test_recovery_uses_bounded_pages_instead_of_hydrating_large_thread(tmp_path: Path):
    with host(tmp_path, "large") as rpc:
        assert rpc.find_attempt("thread-one", "attempt-old") == ("turn-old", "completed")
    history_calls = requests(tmp_path)
    assert not any(call["method"] == "thread/read" and
                   call["params"].get("includeTurns") is True for call in history_calls)
    turn_pages = [call for call in history_calls if call["method"] == "thread/turns/list"]
    item_pages = [call for call in history_calls if call["method"] == "thread/items/list"]
    assert turn_pages and item_pages
    assert all(call["params"].get("sortDirection") == "desc" and
               call["params"].get("itemsView") == "notLoaded" and
               0 < call["params"].get("limit", 0) <= 100 for call in turn_pages)
    assert all(call["params"].get("sortDirection") == "asc" and
               call["params"].get("turnId") and
               0 < call["params"].get("limit", 0) <= 100 for call in item_pages)


def test_missing_attempt_marker_fails_closed_after_bounded_pages(tmp_path: Path):
    with host(tmp_path, "missing") as rpc:
        assert rpc.find_attempt("thread-one", "attempt-old") is None
    assert all(call["method"] != "thread/read" or
               call["params"].get("includeTurns") is not True for call in requests(tmp_path))


def test_explicit_rejection_is_distinct_from_uncertain_transport_loss(tmp_path: Path):
    with host(tmp_path, "reject") as rpc:
        with pytest.raises(CodexHostRejected, match="thread is active"):
            rpc.start_turn("thread-one", "wake")
    with host(tmp_path, "drop") as rpc:
        with pytest.raises(TurnStartUncertain):
            rpc.start_turn("thread-one", "wake")


def test_real_subprocess_stays_alive_until_delayed_terminal_turn(tmp_path: Path):
    with host(tmp_path, "delayed") as rpc:
        turn_id = rpc.start_turn("thread-one", "wake")
        assert rpc.proc is not None and rpc.proc.poll() is None
        started = time.monotonic()
        assert rpc.await_turn_terminal("thread-one", turn_id, max_seconds=1) == "completed"
        assert time.monotonic() - started >= 0.15
        assert rpc.proc is not None and rpc.proc.poll() is None


def test_overdue_turn_is_interrupted_before_transport_teardown(tmp_path: Path):
    with host(tmp_path, "stall") as rpc:
        turn_id = rpc.start_turn("thread-one", "wake")
        assert rpc.await_turn_terminal("thread-one", turn_id, max_seconds=0.01) == "interrupted"
        assert rpc.proc is not None and rpc.proc.poll() is None


def test_approval_request_after_acceptance_is_rejected_and_turn_drains_to_terminal(tmp_path: Path):
    with host(tmp_path, "approval") as rpc:
        turn_id = rpc.start_turn("thread-one", "wake")
        assert rpc.await_turn_terminal("thread-one", turn_id, max_seconds=1) == "interrupted"
        assert rpc.proc is not None and rpc.proc.poll() is None
    history_calls = requests(tmp_path)
    methods = [call["method"] for call in history_calls]
    assert methods.index("turn/start") < methods.index("approval-response") < methods.index("turn/interrupt")
    assert next(call for call in history_calls if call["method"] == "approval-response")["error"]


def test_approval_request_before_start_reply_interrupts_exact_accepted_turn(tmp_path: Path):
    with host(tmp_path, "approval-early") as rpc:
        turn_id = rpc.start_turn("thread-one", "wake")
        assert turn_id == "turn-new"
        assert rpc.await_turn_terminal("thread-one", turn_id, max_seconds=1) == "interrupted"
        assert rpc.proc is not None and rpc.proc.poll() is None
    history_calls = requests(tmp_path)
    methods = [call["method"] for call in history_calls]
    assert methods.index("turn/start") < methods.index("approval-response") < methods.index("turn/interrupt")
    interrupt = next(call for call in history_calls if call["method"] == "turn/interrupt")
    assert interrupt["params"] == {"threadId": "thread-one", "turnId": "turn-new"}


def test_approval_request_for_other_turn_cannot_interrupt_current_turn(tmp_path: Path):
    with host(tmp_path, "approval-other") as rpc:
        turn_id = rpc.start_turn("thread-one", "wake")
        assert rpc.await_turn_terminal("thread-one", turn_id, max_seconds=1) == "completed"
    history_calls = requests(tmp_path)
    assert any(call["method"] == "approval-response" and call["error"] for call in history_calls)
    assert all(call["method"] != "turn/interrupt" for call in history_calls)


def test_approval_near_deadline_requests_exact_interrupt_only_once(tmp_path: Path):
    with host(tmp_path, "approval-grace") as rpc:
        turn_id = rpc.start_turn("thread-one", "wake")
        assert rpc.await_turn_terminal("thread-one", turn_id, max_seconds=0.1) == "interrupted"
        assert rpc.proc is not None and rpc.proc.poll() is None
        time.sleep(0.05)
    history_calls = requests(tmp_path)
    interrupts = [call for call in history_calls if call["method"] == "turn/interrupt"]
    assert len(interrupts) == 1
    assert interrupts[0]["params"] == {"threadId": "thread-one", "turnId": "turn-new"}


def test_terminal_notification_before_interrupt_error_wins(tmp_path: Path):
    with host(tmp_path, "terminal-before-error") as rpc:
        turn_id = rpc.start_turn("thread-one", "wake")
        assert rpc.await_turn_terminal("thread-one", turn_id, max_seconds=0.01) == "interrupted"
        assert rpc.proc is not None and rpc.proc.poll() is None
    interrupts = [call for call in requests(tmp_path) if call["method"] == "turn/interrupt"]
    assert len(interrupts) == 1
    assert interrupts[0]["params"] == {"threadId": "thread-one", "turnId": "turn-new"}


def test_malformed_frame_after_acceptance_drains_exact_turn_before_transport_close(tmp_path: Path):
    with host(tmp_path, "malformed-after-accept") as rpc:
        turn_id = rpc.start_turn("thread-one", "wake")
        assert rpc.await_turn_terminal("thread-one", turn_id, max_seconds=1) == "interrupted"
        assert rpc.proc is not None and rpc.proc.poll() is None
    interrupts = [call for call in requests(tmp_path) if call["method"] == "turn/interrupt"]
    assert len(interrupts) == 1
    assert interrupts[0]["params"] == {"threadId": "thread-one", "turnId": "turn-new"}


@pytest.mark.parametrize("mode,expected", [("early", "completed"), ("failed", "failed")])
def test_terminal_notification_is_captured_before_or_after_start_reply(tmp_path: Path, mode, expected):
    with host(tmp_path, mode) as rpc:
        turn_id = rpc.start_turn("thread-one", "wake")
        assert rpc.await_turn_terminal("thread-one", turn_id, max_seconds=1) == expected
