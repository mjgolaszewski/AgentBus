"""Configuration for the single AgentBus service process."""

from __future__ import annotations

import hmac
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
    federation_config: Path | None = None
    local_delivery_version: int | None = None
    slack_channels: tuple[str, ...] = ()
    local_delivery_outstanding_limit: int = 1024
    local_delivery_retained_limit: int = 65536
    local_delivery_observation_limit: int = 131072
    slack_command: str = "/agentbus"

    @property
    def channels(self) -> tuple[str, ...]:
        return self.slack_channels or (self.slack_channel,)

    def __post_init__(self):
        if not isinstance(self.slack_command, str) or not re.fullmatch(r"/[a-z][a-z0-9_-]{0,31}", self.slack_command):
            raise ValueError("AGENTBUS_SLACK_COMMAND must be a slash followed by 1–32 lowercase command characters")
        from src.agentbus_local_delivery_service import validate_limits
        validate_limits(self.local_delivery_outstanding_limit, self.local_delivery_retained_limit,
                        self.local_delivery_observation_limit)
        if self.local_delivery_version is not None and (type(self.local_delivery_version) is not int or self.local_delivery_version != 1):
            raise ValueError('Unsupported local delivery version')

    def database_bytes(self) -> int:
        """Report current SQLite footprint, including its WAL sidecars."""
        return sum(path.stat().st_size for suffix in ("", "-wal", "-shm")
                   if (path := Path(str(self.db_path) + suffix)).exists())

    @classmethod
    def from_env(cls) -> Settings:
        names = ("API_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")
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
        single = os.environ.get("AGENTBUS_SLACK_CHANNEL")
        multiple = os.environ.get("AGENTBUS_SLACK_CHANNELS")
        if single is not None and multiple is not None:
            raise ValueError("Do not configure both channel variables together")
        from src.agentbus_channel_store_service import ChannelStore
        channels = ChannelStore.validate_channels(tuple(c.strip() for c in (multiple if multiple is not None else single or "").split(',')))
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
        federation_config = os.environ.get('AGENTBUS_FEDERATION_CONFIG', '').strip()
        delivery_version = os.environ.get('AGENTBUS_LOCAL_DELIVERY_VERSION', '').strip()
        if delivery_version not in {'', '1'}:
            raise ValueError('AGENTBUS_LOCAL_DELIVERY_VERSION must be 1 when set')
        limits = {}
        for suffix, default in [('outstanding', 1024), ('retained', 65536), ('observation', 131072)]:
            name = 'AGENTBUS_LOCAL_DELIVERY_' + suffix.upper() + '_LIMIT'
            raw = os.environ.get(name, str(default)).strip()
            if not re.fullmatch(r'[0-9]{1,7}', raw):
                raise ValueError(name + ' must be a bounded positive integer')
            limits['local_delivery_' + suffix + '_limit'] = int(raw)
        return cls(api_token=values["api_token"], slack_bot_token=values["slack_bot_token"],
                   slack_app_token=values["slack_app_token"],
                   slack_command=os.environ.get("AGENTBUS_SLACK_COMMAND", "/agentbus").strip(),
                   slack_channel=channels[0] if len(channels)==1 else "",
                   slack_channels=channels if multiple is not None else (),
                   db_path=db_path.expanduser(), operator_token=operator_token,
                   slack_operator_user_ids=users, local_delivery_version=1 if delivery_version else None,
                   federation_config=Path(federation_config).expanduser() if federation_config else None,
                   local_delivery_outstanding_limit=limits['local_delivery_outstanding_limit'],
                   local_delivery_retained_limit=limits['local_delivery_retained_limit'],
                   local_delivery_observation_limit=limits['local_delivery_observation_limit'])
