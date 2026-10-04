"""Minimal host-owned Codex app-server protocol for the optional wake adapter."""

from __future__ import annotations

import json
import os
import select
import subprocess
import time
from typing import Any


class CodexHostError(Exception):
    """The Codex host rejected or could not complete a read/resume operation."""


class TurnStartUncertain(CodexHostError):
    """A turn-start request was sent, but its acceptance is not known."""


class TurnCompletionUncertain(CodexHostError):
    """An accepted turn has no trustworthy terminal host observation."""


class CodexHostRejected(CodexHostError):
    """The host explicitly rejected a request."""


class CodexAppServer:
    """Connect to the local host daemon without owning its threads or credentials."""

    def __init__(self, command: tuple[str, ...] = ("codex", "app-server", "--listen", "stdio://"),
                 timeout: float = 10.0):
        self.command = command
        self.timeout = timeout
        self.proc: subprocess.Popen[bytes] | None = None
        self.buffer = bytearray()
        self.next_id = 1
        self.terminal_turns: dict[str, str] = {}
        self.rejected_turn_requests: set[tuple[str, str]] = set()
        self.interrupt_requested: set[tuple[str, str]] = set()

    def __enter__(self) -> CodexAppServer:
        try:
            self.proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, close_fds=True,
            )
            self.request("initialize", {"clientInfo": {
                "name": "agentbus_codex_wake", "title": "AgentBus Codex Wake Adapter",
                "version": "0.1.0",
            }})
            self._send({"method": "initialized", "params": {}})
        except (OSError, CodexHostError):
            self.__exit__(None, None, None)
            raise CodexHostError("Codex app-server proxy is unavailable or refused initialization") from None
        return self

    def __exit__(self, *_args: object) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)
        self.proc = None

    def _send(self, message: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise CodexHostError("Codex host connection is closed")
        try:
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            self.proc.stdin.flush()
        except (OSError, BrokenPipeError):
            raise CodexHostError("Codex host connection closed during request") from None

    def _receive(self, deadline: float) -> dict[str, Any]:
        if self.proc is None or self.proc.stdout is None:
            raise CodexHostError("Codex host connection is closed")
        while True:
            if b"\n" in self.buffer:
                line, _, remainder = self.buffer.partition(b"\n")
                self.buffer = bytearray(remainder)
                try:
                    value = json.loads(line)
                except ValueError:
                    raise CodexHostError("Codex host returned invalid JSON") from None
                if not isinstance(value, dict):
                    raise CodexHostError("Codex host returned an invalid frame")
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexHostError("Codex host request timed out")
            readable, _, _ = select.select([self.proc.stdout], [], [], remaining)
            if not readable:
                raise CodexHostError("Codex host request timed out")
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise CodexHostError("Codex host connection closed")
            self.buffer.extend(chunk)
            if len(self.buffer) > 2_000_000:
                raise CodexHostError("Codex host frame exceeded size limit")

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        request_id = self.next_id
        self.next_id += 1
        self._send({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            frame = self._receive(deadline)
            if frame.get("id") == request_id and "method" not in frame:
                if "error" in frame:
                    error = frame["error"]
                    detail = error.get("message", "unknown error") if isinstance(error, dict) else "unknown error"
                    raise CodexHostRejected(f"Codex host rejected {method}: {detail}")
                result = frame.get("result")
                if not isinstance(result, dict):
                    raise CodexHostError(f"Codex host returned no result for {method}")
                return result
            if "method" in frame and "id" in frame:
                self._reject_server_request(frame)
                continue
            self._observe_terminal(frame)

    def _reject_server_request(self, frame: dict[str, Any]) -> None:
        params = frame.get("params")
        if isinstance(params, dict):
            thread_id, turn_id = params.get("threadId"), params.get("turnId")
            if isinstance(thread_id, str) and isinstance(turn_id, str):
                self.rejected_turn_requests.add((thread_id, turn_id))
        self._send({"id": frame["id"], "error": {"code": -32601,
                                                 "message": "AgentBus wake adapter cannot approve host requests"}})

    def _request_interrupt(self, thread_id: str, turn_id: str) -> None:
        key = (thread_id, turn_id)
        if key in self.interrupt_requested or turn_id in self.terminal_turns:
            return
        self.interrupt_requested.add(key)
        try:
            self.request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})
        except CodexHostError:
            # A terminal notification may race an already-terminal rejection.
            # The caller still waits for the exact terminal fact or times out.
            pass

    def _observe_terminal(self, frame: dict[str, Any]) -> None:
        if frame.get("method") != "turn/completed":
            return
        params = frame.get("params")
        turn = params.get("turn") if isinstance(params, dict) else None
        if (isinstance(turn, dict) and isinstance(turn.get("id"), str) and
                turn.get("status") in {"completed", "interrupted", "failed"}):
            self.terminal_turns[turn["id"]] = turn["status"]

    def _terminal_until(self, thread_id: str, turn_id: str, deadline: float) -> str:
        if (thread_id, turn_id) in self.rejected_turn_requests and turn_id not in self.terminal_turns:
            self._request_interrupt(thread_id, turn_id)
        while turn_id not in self.terminal_turns:
            frame = self._receive(deadline)
            if "method" in frame and "id" in frame:
                self._reject_server_request(frame)
                if (thread_id, turn_id) in self.rejected_turn_requests:
                    self._request_interrupt(thread_id, turn_id)
                continue
            self._observe_terminal(frame)
        self.rejected_turn_requests.discard((thread_id, turn_id))
        self.interrupt_requested.discard((thread_id, turn_id))
        return self.terminal_turns.pop(turn_id)

    def await_turn_terminal(self, thread_id: str, turn_id: str, *, max_seconds: float = 3600) -> str:
        """Keep the owning stdio transport open through exact terminal status."""
        try:
            return self._terminal_until(thread_id, turn_id, time.monotonic() + max_seconds)
        except CodexHostError:
            # A malformed frame can be recoverable while the accepted turn is
            # still running. Give every first wait error the same exact-turn
            # interrupt and terminal grace as a timeout.
            pass
        self._request_interrupt(thread_id, turn_id)
        try:
            return self._terminal_until(thread_id, turn_id, time.monotonic() + self.timeout)
        except CodexHostError:
            raise TurnCompletionUncertain("Codex turn did not reach a terminal host state") from None

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        thread = self.request("thread/read", {"threadId": thread_id,
                                               "includeTurns": False}).get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise CodexHostError("Codex host did not return the exact bound thread")
        return thread

    def resume(self, thread_id: str) -> None:
        # Resume responses otherwise hydrate the entire conversation. Large
        # saved threads can exceed our bounded host frame before turn/start.
        thread = self.request("thread/resume", {"threadId": thread_id,
                                                 "excludeTurns": True}).get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise CodexHostError("Codex host resumed a different thread")

    def start_turn(self, thread_id: str, prompt: str) -> str:
        try:
            result = self.request("turn/start", {"threadId": thread_id,
                                                  "input": [{"type": "text", "text": prompt}]})
        except CodexHostRejected:
            raise
        except CodexHostError:
            # The request might have crossed the transport before any error or
            # timeout. Only a later host-history reconciliation can prove truth.
            raise TurnStartUncertain("Codex turn-start outcome is uncertain; reconcile before retry") from None
        turn = result.get("turn")
        if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
            raise TurnStartUncertain("Codex turn-start response omitted a turn ID")
        return turn["id"]

    def find_attempt(self, thread_id: str, attempt_id: str) -> tuple[str, str] | None:
        # A saved rollout may be hundreds of megabytes. Inspect only recent
        # turn summaries and their first bounded item pages; never hydrate the
        # full thread to reconcile an uncertain, just-started attempt.
        marker = f"AGENTBUS_WAKE_ATTEMPT={attempt_id}"
        turn_cursor: str | None = None
        for _ in range(3):
            params: dict[str, Any] = {"threadId": thread_id, "limit": 8,
                                      "sortDirection": "desc", "itemsView": "notLoaded"}
            if turn_cursor is not None:
                params["cursor"] = turn_cursor
            page = self.request("thread/turns/list", params)
            turns = page.get("data")
            if not isinstance(turns, list):
                raise CodexHostError("Codex host returned invalid turn pagination")
            for turn in turns:
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    raise CodexHostError("Codex host returned an invalid turn summary")
                item_cursor: str | None = None
                for _ in range(2):
                    item_params: dict[str, Any] = {"threadId": thread_id,
                                                   "turnId": turn["id"], "limit": 8,
                                                   "sortDirection": "asc"}
                    if item_cursor is not None:
                        item_params["cursor"] = item_cursor
                    item_page = self.request("thread/items/list", item_params)
                    entries = item_page.get("data")
                    if not isinstance(entries, list):
                        raise CodexHostError("Codex host returned invalid item pagination")
                    for entry in entries:
                        item = entry.get("item") if isinstance(entry, dict) else None
                        if (isinstance(item, dict) and item.get("type") == "userMessage" and
                                marker in json.dumps(item)):
                            return turn["id"], str(turn.get("status", "unknown"))
                    next_item = item_page.get("nextCursor")
                    if next_item is None:
                        break
                    if not isinstance(next_item, str) or next_item == item_cursor:
                        raise CodexHostError("Codex host returned an invalid item cursor")
                    item_cursor = next_item
            next_turn = page.get("nextCursor")
            if next_turn is None:
                break
            if not isinstance(next_turn, str) or next_turn == turn_cursor:
                raise CodexHostError("Codex host returned an invalid turn cursor")
            turn_cursor = next_turn
        return None
