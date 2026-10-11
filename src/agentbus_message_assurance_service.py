"""Route and sender-provenance rules for the service message plane."""

from __future__ import annotations

import re

PREFIX = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
REPO = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
LEGACY_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def route_parts(value: str) -> tuple[str | None, str, str] | None:
    """Parse both retained repo:name routes and new prefix:repo:name routes."""
    if len(value) > 80:
        return None
    parts = value.split(":")
    if len(parts) == 2:
        prefix = None
        repo, name = parts
    elif len(parts) == 3:
        prefix, repo, name = parts
        if prefix in {"slack", "all"} or not PREFIX.fullmatch(prefix):
            return None
    else:
        return None
    if repo in {"slack", "all"} or not REPO.fullmatch(repo) or not NAME.fullmatch(name):
        return None
    return prefix, repo, name


def canonical_route(value: str) -> bool:
    return route_parts(value) is not None


def valid_new_message_routes(sender: str, recipient: str) -> bool:
    def valid(value: str) -> bool:
        return canonical_route(value) or (":" not in value and bool(LEGACY_LABEL.fullmatch(value)))

    return valid(sender) and (recipient == "all" or valid(recipient))
