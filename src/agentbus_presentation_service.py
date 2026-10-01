"""Escape invisible controls in Slack's human-facing projection only."""

from __future__ import annotations

import unicodedata


def visible_text(value: str) -> str:
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) in {"Cc", "Cf"} and char not in "\n\t"
        else char for char in value
    )
