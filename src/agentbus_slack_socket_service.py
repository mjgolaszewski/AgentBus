"""Slack Socket Mode ingress and bounded operator-command dispatch."""

from __future__ import annotations

import concurrent.futures
import logging
import sqlite3

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.client import BaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from src.agentbus_settings_service import Settings
from src.agentbus_slack_command_service import execute_command
from src.agentbus_slack_reply_service import append_slack

LOGGER = logging.getLogger("agentbus")


class SlackReceiver:
    def __init__(self, store, channel: str, trusted_bot_id: str,
                 settings: Settings | None = None):
        self.store, self.channel, self.trusted_bot_id = store, channel, trusted_bot_id
        self.settings = settings
        self._commands = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def _finish_command(self, client: BaseSocketModeClient, payload: dict) -> None:
        try:
            result = execute_command(payload, self.settings, self.store, client.web_client)
            client.web_client.chat_postEphemeral(
                channel=payload["channel_id"], user=payload["user_id"], text=result[:3900],
            )
        except Exception:
            LOGGER.error("Could not complete AgentBus Slack command; outcome may be uncertain")
            try:
                client.web_client.chat_postEphemeral(
                    channel=payload["channel_id"], user=payload["user_id"],
                    text="AgentBus command outcome is uncertain. Inspect status and the channel before retrying.",
                )
            except Exception:
                LOGGER.error("Could not report uncertain AgentBus Slack command outcome")

    def __call__(self, client: BaseSocketModeClient, request: SocketModeRequest) -> None:
        if request.type == "slash_commands":
            payload = request.payload if isinstance(request.payload, dict) else {}
            command_id = str(payload.get("trigger_id") or request.envelope_id)
            if len(command_id) > 200:
                command_id = request.envelope_id
            try:
                first = self.store.claim_slash_command(command_id)
            except sqlite3.Error:
                LOGGER.error("Could not durably record Slack command; withholding acknowledgement")
                return
            client.send_socket_mode_response(SocketModeResponse(
                envelope_id=request.envelope_id,
                payload={"response_type": "ephemeral", "text": (
                    "Checking AgentBus command…" if first else
                    "This command was already received. Inspect its result before retrying.")},
            ))
            if first and self.settings is not None and payload:
                self._commands.submit(self._finish_command, client, payload)
            return
        if request.type == "events_api":
            from src.agentbus_service import normalize_event

            event = request.payload.get("event")
            normalized = (
                normalize_event(event, self.channel, self.trusted_bot_id)
                if isinstance(event, dict) else None
            )
            if normalized is not None:
                ts, message = normalized
                try:
                    append_slack(self.store, self.channel, ts, message)
                except (sqlite3.Error, OSError):
                    LOGGER.error("Could not durably store Slack event; withholding acknowledgement")
                    return
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))


def build_socket(settings: Settings, receiver: SlackReceiver) -> SocketModeClient:
    client = SocketModeClient(
        app_token=settings.slack_app_token,
        web_client=WebClient(token=settings.slack_bot_token, timeout=15, retry_handlers=[]),
        concurrency=1,
    )
    client.socket_mode_request_listeners.append(receiver.__call__)
    return client
