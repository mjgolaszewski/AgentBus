"""Contract probes for the experimental executable override only."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

WRAPPER = Path(__file__).resolve().parents[1] / "experiments/codex-executable-wrapper/codex-wrapper"


def fake_binary(tmp_path: Path) -> Path:
    script = tmp_path / "real-codex"
    script.write_text("#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n")
    script.chmod(0o700)
    return script


def call(tmp_path: Path, args: list[str], *, live_socket: bool = True,
         binary: Path | None = None) -> subprocess.CompletedProcess[str]:
    target = binary or fake_binary(tmp_path)
    socket_path = tmp_path / "owner.sock"
    if live_socket:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(socket_path))
        socket_path.chmod(0o600)
        listener.listen()
    else:
        listener = None
    try:
        env = dict(os.environ, AGENTBUS_EXPERIMENTAL_CODEX_BINARY=str(target),
                   AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET=str(socket_path))
        return subprocess.run([sys.executable, str(WRAPPER), *args], env=env,
                              text=True, capture_output=True, timeout=5)
    finally:
        if listener:
            listener.close()


def test_observed_extension_launch_goes_to_existing_control_socket(tmp_path: Path) -> None:
    args = ["-c", "features.code_mode_host=true", "app-server", "--analytics-default-enabled"]
    result = call(tmp_path, args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["-c", "features.code_mode_host=true", "app-server",
                                         "proxy", "--sock", str(tmp_path / "owner.sock")]


def test_other_cli_commands_delegate_unchanged(tmp_path: Path) -> None:
    result = call(tmp_path, ["--version"], live_socket=False)
    assert result.returncode == 0
    assert json.loads(result.stdout) == ["--version"]


def test_missing_control_socket_never_starts_new_host(tmp_path: Path) -> None:
    result = call(tmp_path, ["app-server"], live_socket=False)
    assert result.returncode == 78
    assert "refusing an independent host" in result.stderr
    assert not result.stdout


def test_unrecognized_host_flags_fail_closed(tmp_path: Path) -> None:
    result = call(tmp_path, ["app-server", "--listen", "ws://127.0.0.1:9000"])
    assert result.returncode == 78
    assert "unsupported app-server launch flags" in result.stderr
    assert not result.stdout


def test_configured_binary_cannot_be_wrapper(tmp_path: Path) -> None:
    result = call(tmp_path, ["--version"], binary=WRAPPER)
    assert result.returncode == 78
    assert "points back" in result.stderr


def test_broken_real_binary_fails_without_traceback(tmp_path: Path) -> None:
    binary = tmp_path / "broken-codex"
    binary.write_text("not an executable format\n")
    binary.chmod(0o700)
    result = call(tmp_path, ["--version"], live_socket=False, binary=binary)
    assert result.returncode == 78
    assert "configured Codex binary could not start" in result.stderr
    assert "Traceback" not in result.stderr


def test_unsafe_socket_mode_fails_closed(tmp_path: Path) -> None:
    listener = socket.socket(socket.AF_UNIX)
    path = tmp_path / "owner.sock"
    listener.bind(str(path))
    path.chmod(0o666)
    try:
        env = dict(os.environ, AGENTBUS_EXPERIMENTAL_CODEX_BINARY=str(fake_binary(tmp_path)),
                   AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET=str(path))
        result = subprocess.run([sys.executable, str(WRAPPER), "app-server"], env=env,
                                text=True, capture_output=True, timeout=5)
        assert result.returncode == 78
        assert "accessible to group or other" in result.stderr
    finally:
        listener.close()


def test_unsafe_socket_parent_fails_closed(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)
    path = parent / "owner.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(path))
    path.chmod(0o600)
    try:
        env = dict(os.environ, AGENTBUS_EXPERIMENTAL_CODEX_BINARY=str(fake_binary(tmp_path)),
                   AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET=str(path))
        result = subprocess.run([sys.executable, str(WRAPPER), "app-server"], env=env,
                                text=True, capture_output=True, timeout=5)
        assert result.returncode == 78
        assert "parent is not private" in result.stderr
    finally:
        listener.close()
