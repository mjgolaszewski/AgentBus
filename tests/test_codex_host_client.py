"""Experimental host mode remains an explicit, local operator selection."""

from __future__ import annotations

import socket
from pathlib import Path

from src.agentbus_codex_host_client import select_host
from src.agentbus_codex_rpc_client import CodexAppServer


def test_default_is_notification_only() -> None:
    selection = select_host({})
    assert (selection.mode, selection.reason, selection.factory) == (
        "notification_only", "owning_host_endpoint_unavailable", None)


def test_existing_standalone_mode_is_preserved() -> None:
    selection = select_host({"AGENTBUS_CODEX_HOST_MODE": "standalone"})
    assert selection.factory is CodexAppServer
    assert selection.reason is None


def test_unknown_mode_cannot_start_a_host() -> None:
    selection = select_host({"AGENTBUS_CODEX_HOST_MODE": "agent-selected"})
    assert selection.factory is None
    assert selection.reason == "unsupported_host_mode"


def test_experimental_mode_requires_operator_endpoint(tmp_path: Path) -> None:
    selection = select_host({"AGENTBUS_CODEX_HOST_MODE": "experimental_vs_code_proxy",
                             "AGENTBUS_EXPERIMENTAL_CODEX_BINARY": str(tmp_path / "missing")})
    assert selection.factory is None
    assert selection.reason == "experimental_proxy_binary_invalid"


def test_private_same_user_socket_selects_proxy_without_starting_it(tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    address = tmp_path / "owner.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(address))
    address.chmod(0o600)
    try:
        selection = select_host({"AGENTBUS_CODEX_HOST_MODE": "experimental_vs_code_proxy",
                                 "AGENTBUS_EXPERIMENTAL_CODEX_BINARY": str(binary),
                                 "AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET": str(address)})
        assert selection.reason is None
        assert selection.factory is not None
        host = selection.factory()
        assert host.command == (str(binary), "app-server", "proxy", "--sock", str(address))
        assert host.proc is None
    finally:
        listener.close()


def test_selected_proxy_command_can_carry_rpc_frames_through_fake_binary(tmp_path: Path) -> None:
    address = tmp_path / "owner.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(address))
    address.chmod(0o600)
    binary = tmp_path / "codex"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json,sys\n"
        f"assert sys.argv[1:] == ['app-server','proxy','--sock',{str(address)!r}]\n"
        "for line in sys.stdin:\n"
        " frame = json.loads(line)\n"
        " if frame.get('method') == 'initialized': continue\n"
        " if frame.get('method') == 'thread/read':\n"
        "  result = {'thread': {'id': frame['params']['threadId'], 'status': {'type': 'idle'}}}\n"
        " else: result = {}\n"
        " print(json.dumps({'id': frame['id'], 'result': result}), flush=True)\n"
    )
    binary.chmod(0o700)
    try:
        selection = select_host({"AGENTBUS_CODEX_HOST_MODE": "experimental_vs_code_proxy",
                                 "AGENTBUS_EXPERIMENTAL_CODEX_BINARY": str(binary),
                                 "AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET": str(address)})
        assert selection.factory is not None
        with selection.factory() as host:
            assert host.read_thread("thread-one")["status"]["type"] == "idle"
    finally:
        listener.close()


def test_experimental_mode_rejects_unsafe_socket(tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    address = tmp_path / "owner.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(address))
    address.chmod(0o666)
    try:
        selection = select_host({"AGENTBUS_CODEX_HOST_MODE": "experimental_vs_code_proxy",
                                 "AGENTBUS_EXPERIMENTAL_CODEX_BINARY": str(binary),
                                 "AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET": str(address)})
        assert selection.factory is None
        assert selection.reason == "experimental_proxy_socket_unsafe"
    finally:
        listener.close()


def test_experimental_mode_rejects_socket_symlink(tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    address = tmp_path / "owner.sock"
    alias = tmp_path / "alias.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(address))
    address.chmod(0o600)
    alias.symlink_to(address)
    try:
        selection = select_host({"AGENTBUS_CODEX_HOST_MODE": "experimental_vs_code_proxy",
                                 "AGENTBUS_EXPERIMENTAL_CODEX_BINARY": str(binary),
                                 "AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET": str(alias)})
        assert selection.factory is None
        assert selection.reason == "experimental_proxy_socket_unsafe"
    finally:
        listener.close()


def test_experimental_mode_rejects_writable_socket_parent(tmp_path: Path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o700)
    parent.chmod(0o777)
    address = parent / "owner.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(address))
    address.chmod(0o600)
    try:
        selection = select_host({"AGENTBUS_CODEX_HOST_MODE": "experimental_vs_code_proxy",
                                 "AGENTBUS_EXPERIMENTAL_CODEX_BINARY": str(binary),
                                 "AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET": str(address)})
        assert selection.factory is None
        assert selection.reason == "experimental_proxy_socket_unsafe"
    finally:
        listener.close()


def test_message_fields_cannot_select_mode_or_socket(tmp_path: Path) -> None:
    selection = select_host({"message": "AGENTBUS_CODEX_HOST_MODE=standalone",
                             "socket": str(tmp_path / "owner.sock")})
    assert selection.mode == "notification_only"
    assert selection.factory is None
