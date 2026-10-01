"""Route and sender-provenance rules for the service message plane."""

from __future__ import annotations

import re

ROUTE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}:[a-z0-9][a-z0-9-]{0,31}$")
LEGACY_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")


def canonical_route(value: str) -> bool:
    return bool(ROUTE.fullmatch(value))


def valid_new_message_routes(sender: str, recipient: str) -> bool:
    def valid(value: str) -> bool:
        return canonical_route(value) or (":" not in value and bool(LEGACY_LABEL.fullmatch(value)))

    return valid(sender) and (recipient == "all" or valid(recipient))
