"""Explicit host selection for the optional Codex wake client."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.agentbus_codex_rpc_client import CodexAppServer


@dataclass(frozen=True)
class HostSelection:
    mode: str
    reason: str | None
    factory: Callable[[], CodexAppServer] | None


def select_host(values: dict[str, str]) -> HostSelection:
    """Select only operator-configured transports; never derive an endpoint from a message."""
    mode = values.get("AGENTBUS_CODEX_HOST_MODE") or "notification_only"
    if mode == "standalone":
        return HostSelection(mode, None, CodexAppServer)
    if mode != "experimental_vs_code_proxy":
        reason = "owning_host_endpoint_unavailable" if mode == "notification_only" else "unsupported_host_mode"
        return HostSelection(mode, reason, None)

    raw_binary = values.get("AGENTBUS_EXPERIMENTAL_CODEX_BINARY", "")
    binary = Path(raw_binary)
    if not binary.is_absolute() or not binary.is_file() or not os.access(binary, os.X_OK):
        return HostSelection(mode, "experimental_proxy_binary_invalid", None)

    raw_socket = values.get("AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET", "")
    socket = Path(raw_socket)
    if not socket.is_absolute():
        return HostSelection(mode, "experimental_proxy_socket_invalid", None)
    try:
        parent = socket.parent.lstat()
        info = socket.lstat()
    except OSError:
        return HostSelection(mode, "experimental_proxy_socket_unavailable", None)
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        return HostSelection(mode, "experimental_proxy_socket_unsafe", None)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        return HostSelection(mode, "experimental_proxy_socket_unsafe", None)

    command = (str(binary), "app-server", "proxy", "--sock", str(socket))
    return HostSelection(mode, None, lambda: CodexAppServer(command=command))
