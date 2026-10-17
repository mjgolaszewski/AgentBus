"""An authenticated Slack channel never grants authority to foreign route labels."""
from fastapi import HTTPException

from src.agentbus_channel_api_service import require_target
from src.agentbus_message_assurance_service import canonical_route
from src.agentbus_slack_reply_service import append_slack


def append_scoped(store,channel,ts,message,bridge=None):
    with store._lock:
        if message.reply_to_cursor is not None:
            parent=store.channels.internal_cursor(channel,message.reply_to_cursor)
            if parent is None:
                return None
            message.reply_to_cursor=parent
        if canonical_route(message.sender):
            if not store.channels.local_target(channel,message.sender):
                return None
        try:
            require_target(store,channel,message,bridge)
        except (HTTPException,PermissionError):
            return None
        return append_slack(store,channel,ts,message)
