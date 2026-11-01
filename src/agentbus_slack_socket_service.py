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

from src.agentbus_federation_inbox_service import InboxProjectionError
from src.agentbus_federation_service import CustodyCapacityError, DirectoryCapacityError
from src.agentbus_local_delivery_service import UnretainedDeliveryEcho
from src.agentbus_settings_service import Settings
from src.agentbus_slack_command_service import execute_command

LOGGER = logging.getLogger("agentbus")


class SlackReceiver:
    def __init__(self, store, channel: str, trusted_bot_id: str,
                 settings: Settings | None = None, federation_bridge=None, *, dashboard=None):
        self.store, self.trusted_bot_id = store, trusted_bot_id
        self.channels = (channel,) if isinstance(channel, str) else tuple(channel)
        self.channel = self.channels[0]
        self.settings = settings
        self.federation_bridge = federation_bridge
        self.dashboard = dashboard
        self._commands = concurrent.futures.ThreadPoolExecutor(max_workers=2)

    def _finish_command(self, client: BaseSocketModeClient, payload: dict) -> None:
        try:
            from src.agentbus_status_api_service import service_dashboard
            dashboard = self.dashboard or (lambda: service_dashboard(
                self.settings, self.store, socket=client, bridge=self.federation_bridge))
            result = execute_command(payload, self.settings, self.store, client.web_client, dashboard=dashboard)
            self._post_reply(client, payload['channel_id'], payload['user_id'], result)
        except Exception:
            LOGGER.error("Could not complete AgentBus Slack command; outcome may be uncertain")
            try:
                client.web_client.chat_postEphemeral(
                    channel=payload["channel_id"], user=payload["user_id"],
                    text="AgentBus command outcome is uncertain. Inspect status and the channel before retrying.",
                )
            except Exception:
                LOGGER.error("Could not report uncertain AgentBus Slack command outcome")

    @staticmethod
    def _post_reply(client, channel: str, user: str, result: str) -> None:
        from src.agentbus_presentation_service import command_blocks, slack_escape
        from src.agentbus_slack_ui_service import OperatorReply
        blocks = result.blocks if isinstance(result, OperatorReply) else command_blocks(result)
        display = result.text if isinstance(result, OperatorReply) else result
        client.web_client.chat_postEphemeral(channel=channel, user=user,
            text=slack_escape(display)[:3900], blocks=blocks, parse='none')
        if isinstance(result, OperatorReply):
            for command in result.command_messages:
                # The whole message is the exact copyable command, without a heading or markdown.
                client.web_client.chat_postEphemeral(channel=channel, user=user, text=command, parse='none')

    def _finish_interaction(self, client, payload: dict, principal: tuple[str, str]) -> None:
        from src.agentbus_slack_ui_commands_service import execute_interaction
        user, channel = principal
        try:
            result = execute_interaction(payload, self.settings, self.store, client.web_client)
            self._post_reply(client, channel, user, result)
        except Exception:
            LOGGER.error('Could not finish AgentBus operator button; inspect status before retrying')
            try:
                self._post_reply(client, channel, user,
                    'Button outcome is uncertain. Review enrollments or results before retrying.')
            except Exception:
                LOGGER.error('Could not report uncertain AgentBus operator button outcome')

    def __call__(self, client: BaseSocketModeClient, request: SocketModeRequest) -> None:
        if request.type == 'interactive':
            from src.agentbus_slack_ui_commands_service import interaction_principal
            payload = request.payload if isinstance(request.payload, dict) else {}
            principal = interaction_principal(payload, self.settings)
            client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            if principal is not None:
                self._commands.submit(self._finish_interaction, client, payload, principal)
            return
        if request.type == "slash_commands":
            payload = request.payload if isinstance(request.payload, dict) else {}
            if payload.get('channel_id') not in self.channels:
                client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
                return
            if self.settings is not None and payload.get('command') != self.settings.slack_command:
                client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id,
                    payload={'response_type': 'ephemeral', 'text': 'Unsupported AgentBus slash command.'}))
                return
            if (self.settings is None
                    or payload.get('user_id') not in self.settings.slack_operator_user_ids):
                client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id,
                    payload={'response_type':'ephemeral','text':'AgentBus operator command access is not enabled for this Slack user.'}))
                return
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
            channel = event.get('channel') if isinstance(event, dict) else None
            if channel not in self.channels:
                client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
                return
            try:
                if isinstance(event, dict) and self.federation_bridge is not None and self.federation_bridge.accept_event(event):
                    client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
                    return
            except (sqlite3.Error, OSError, CustodyCapacityError, DirectoryCapacityError, InboxProjectionError):
                LOGGER.error('Could not persist federation event; withholding acknowledgement')
                return
            normalized = (
                normalize_event(event, channel, self.trusted_bot_id)
                if isinstance(event, dict) else None
            )
            if normalized is not None:
                ts, message = normalized
                try:
                    if (self.trusted_bot_id and isinstance(event, dict) and event.get('bot_id') == self.trusted_bot_id and
                            self.store.local_delivery.accept_event_echo(channel, ts, event.get('text'),
                                event.get('blocks'), event.get('thread_ts'),
                                require_retained=self.store.lifecycle.enabled)):
                        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
                        return
                    from src.agentbus_channel_ingress_service import append_scoped
                    append_scoped(self.store, channel, ts, message, self.federation_bridge,
                                  operator_user_ids=self.settings.slack_operator_user_ids if self.settings else frozenset())
                except UnretainedDeliveryEcho:
                    client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
                    return
                except (sqlite3.Error, OSError, ValueError):
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
