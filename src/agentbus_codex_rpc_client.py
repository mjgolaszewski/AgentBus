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
                raise CodexHostError("Codex host requested interactive input; adapter cannot answer it")
            # Other notifications are host observations, not request results.

    def read_thread(self, thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
        thread = self.request("thread/read", {"threadId": thread_id,
                                               "includeTurns": include_turns}).get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id:
            raise CodexHostError("Codex host did not return the exact bound thread")
        return thread

    def resume(self, thread_id: str) -> None:
        thread = self.request("thread/resume", {"threadId": thread_id}).get("thread")
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
        thread = self.read_thread(thread_id, include_turns=True)
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return None
        marker = f"AGENTBUS_WAKE_ATTEMPT={attempt_id}"
        for turn in reversed(turns):
            if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                continue
            for item in turn.get("items", []):
                if isinstance(item, dict) and item.get("type") == "userMessage" and marker in json.dumps(item):
                    return turn["id"], str(turn.get("status", "unknown"))
        return None
