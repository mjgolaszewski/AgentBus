"""Configuration and HTTP transport for the AgentBus command-line client."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ClientError(Exception):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configuration(*, include_operator: bool = False) -> tuple[Path, dict[str, str]]:
    source = Path(__file__).resolve().parent
    default_project = next(
        (candidate for candidate in (source, source.parent)
         if (candidate / "pyproject.toml").is_file()),
        source,
    )
    project = Path(os.environ.get("AGENTBUS_PROJECT_DIR", str(default_project))).resolve()
    config = Path(os.environ.get("AGENTBUS_CONFIG", str(project / ".env"))).expanduser()
    values: dict[str, str] = {}
    if config.is_file():
        for number, line in enumerate(config.read_text().splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            key, separator, value = line.partition("=")
            key = key.strip()
            if not separator or not key.startswith("AGENTBUS_") or not key.replace("_", "").isalnum():
                raise ClientError(f"Invalid AgentBus setting at {config}:{number}")
            if key == "AGENTBUS_OPERATOR_TOKEN" and not include_operator:
                continue
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                raise ClientError(f"Invalid quoting at {config}:{number}") from None
            if len(parts) > 1:
                raise ClientError(f"Quote values containing spaces at {config}:{number}")
            values[key] = parts[0] if parts else ""
    values.update(os.environ)
    if not include_operator:
        values.pop("AGENTBUS_OPERATOR_TOKEN", None)
    # Existing checkout state retains its location. Fresh installations use a
    # writable per-user directory instead of assuming the source tree is writable.
    legacy_state = project / ".state"
    xdg_state = Path(values.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "agentbus"
    values.setdefault("AGENTBUS_STATE_DIR", str(
        legacy_state if legacy_state.is_dir() and not legacy_state.is_symlink() else xdg_state
    ))
    values.setdefault("AGENTBUS_DB_PATH", str(Path(values["AGENTBUS_STATE_DIR"]) / "messages.sqlite3"))
    values.setdefault("AGENTBUS_HOST", "127.0.0.1")
    values.setdefault("AGENTBUS_PORT", "8766")
    values.setdefault("AGENTBUS_URL", f"http://127.0.0.1:{values['AGENTBUS_PORT']}")
    return project, values


def api(values: dict[str, str], path: str, payload: dict | None = None, *,
        bearer_token: str | None = None, session_token: str | None = None,
        timeout_seconds: float | None = None) -> dict:
    token = bearer_token if bearer_token is not None else values.get("AGENTBUS_API_TOKEN", "")
    if path != "/healthz" and not token:
        raise ClientError("Set AGENTBUS_API_TOKEN in the environment or AgentBus .env file.")
    base = values["AGENTBUS_URL"].rstrip("/")
    try:
        parsed = urlsplit(base)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and parsed.port != 0
    except ValueError:
        raise ClientError("AGENTBUS_URL is not a valid HTTP(S) service URL.") from None
    if not valid or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClientError("AGENTBUS_URL must be an HTTP(S) service URL without embedded credentials.")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ClientError("Use HTTPS for a remote AgentBus URL; HTTP is supported on loopback only.")
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"}
    headers["X-AgentBus-Client-Capabilities"] = "participation-v1"
    if token and path != "/healthz":
        headers["Authorization"] = f"Bearer {token}"
    if session_token is not None:
        headers["X-AgentBus-Session-Token"] = session_token
    request = Request(base + path, data=body, headers=headers)
    try:
        with build_opener(NoRedirects).open(
            request, timeout=timeout_seconds or (3 if path == "/healthz" else 35)
        ) as response:
            return json.load(response)
    except HTTPError as exc:
        retry = exc.headers.get("Retry-After")
        suffix = f"; retry after {retry} seconds" if retry else ""
        claimant = exc.headers.get("X-AgentBus-Claimed-By")
        if exc.code == 409 and claimant and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", claimant):
            suffix += f"; already claimed by {claimant}"
        # Avoid printing upstream/proxy response bodies that might echo credentials.
        raise ClientError(f"AgentBus returned HTTP {exc.code}{suffix}.") from None
    except (URLError, TimeoutError):
        raise ClientError("Cannot reach AgentBus. Check `agentbus status` and AGENTBUS_URL.") from None
    except (ValueError, UnicodeDecodeError):
        raise ClientError("AgentBus returned invalid JSON. Check AGENTBUS_URL.") from None
