"""Configuration for the single AgentBus service process."""

from __future__ import annotations

import hmac
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    api_token: str = field(repr=False)
    slack_bot_token: str = field(repr=False)
    slack_app_token: str = field(repr=False)
    slack_channel: str
    db_path: Path
    operator_token: str | None = field(default=None, repr=False)
    slack_operator_user_ids: frozenset[str] = frozenset()
    slack_channels: frozenset[str] = frozenset()
    slack_repo_channels: dict[str, str] = field(default_factory=dict)

    @property
    def allowed_channels(self) -> frozenset[str]:
        return self.slack_channels | {self.slack_channel}

    def database_bytes(self) -> int:
        """Report current SQLite footprint, including its WAL sidecars."""
        return sum(path.stat().st_size for suffix in ("", "-wal", "-shm")
                   if (path := Path(str(self.db_path) + suffix)).exists())

    @classmethod
    def from_env(cls) -> Settings:
        names = ("API_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL")
        values = {name.lower(): os.environ.get(f"AGENTBUS_{name}", "").strip() for name in names}
        missing = [f"AGENTBUS_{name}" for name in names if not values[name.lower()]]
        if missing:
            raise ValueError("Missing required configuration: " + ", ".join(missing))
        if not re.fullmatch(r"[!-~]{32,}", values["api_token"]):
            raise ValueError("AGENTBUS_API_TOKEN must contain at least 32 printable non-whitespace ASCII characters")
        if not values["slack_bot_token"].startswith("xoxb-"):
            raise ValueError("AGENTBUS_SLACK_BOT_TOKEN must be a bot token (xoxb-)")
        if not values["slack_app_token"].startswith("xapp-"):
            raise ValueError("AGENTBUS_SLACK_APP_TOKEN must be an app token (xapp-)")
        if not re.fullmatch(r"[CG][A-Z0-9]+", values["slack_channel"]):
            raise ValueError("AGENTBUS_SLACK_CHANNEL must be a channel ID, not a name")
        state = Path(os.environ.get("XDG_STATE_HOME", str(Path(os.path.expanduser("~")) / ".local/state")))
        db_path = Path(os.environ.get("AGENTBUS_DB_PATH", str(state / "agentbus/messages.sqlite3")))
        operator_token = os.environ.get("AGENTBUS_OPERATOR_TOKEN", "").strip() or None
        if operator_token is not None:
            if not re.fullmatch(r"[!-~]{32,}", operator_token):
                raise ValueError("AGENTBUS_OPERATOR_TOKEN must contain at least 32 printable non-whitespace ASCII characters")
            if hmac.compare_digest(operator_token, values["api_token"]):
                raise ValueError("AGENTBUS_OPERATOR_TOKEN must differ from AGENTBUS_API_TOKEN")
        raw_users = os.environ.get("AGENTBUS_SLACK_OPERATOR_USER_IDS", "").strip()
        users = frozenset(item.strip() for item in raw_users.split(",") if item.strip())
        if any(not re.fullmatch(r"[UW][A-Z0-9]{2,59}", item) for item in users):
            raise ValueError("AGENTBUS_SLACK_OPERATOR_USER_IDS must contain Slack user IDs")
        channels = frozenset(item.strip() for item in
                             os.environ.get("AGENTBUS_SLACK_CHANNELS", "").split(",") if item.strip())
        if any(not re.fullmatch(r"[CG][A-Z0-9]+", item) for item in channels):
            raise ValueError("AGENTBUS_SLACK_CHANNELS must contain channel IDs")
        try:
            room_map = json.loads(os.environ.get("AGENTBUS_SLACK_REPO_CHANNELS", "{}"))
        except ValueError:
            raise ValueError("AGENTBUS_SLACK_REPO_CHANNELS must be a JSON object") from None
        if not isinstance(room_map, dict) or any(
            not isinstance(repo, str) or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", repo)
            or not isinstance(room, str) or room not in channels | {values["slack_channel"]}
            for repo, room in room_map.items()
        ):
            raise ValueError("AGENTBUS_SLACK_REPO_CHANNELS must map repository names to configured channel IDs")
        return cls(**values, db_path=db_path.expanduser(), operator_token=operator_token,
                   slack_operator_user_ids=users, slack_channels=channels,
                   slack_repo_channels=room_map)
