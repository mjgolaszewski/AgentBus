"""Slack Socket Mode ingress and bounded operator-command dispatch."""

from __future__ import annotations

import concurrent.futures
import logging
import sqlite3
from typing import Any, cast

from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.client import BaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from src.agentbus_peer_directory_service import missing_origin_peer, parse_wire, wire
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

    def _append_message(self, event: dict, channel: str, ts: str, message) -> None:
        stored = append_slack(self.store, channel, ts, message)
        if (self.settings is None or message._sender_assurance != "legacy"
                or message.audience != "unrouted"):
            return
        old_peer = missing_origin_peer(event, channel, self.settings.peer_bot_ids)
        if old_peer is not None:
            self.store.peers.record_missing_origin(*old_peer, stored.received_at)

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

    def _recover_thread(self, client: BaseSocketModeClient, event: dict) -> None:
        """A hidden thread update is a signal, not the reply's message body."""
        from src.agentbus_service import normalize_event

        raw_channel = event.get("channel")
        channel: str = raw_channel if isinstance(raw_channel, str) else ""
        parent = event.get("message")
        root = parent.get("ts") if isinstance(parent, dict) else event.get("thread_ts")
        if not isinstance(root, str):
            raise RuntimeError("Slack thread update lacks a root timestamp")
        cursor = None
        for page_index in range(50):
            page = client.web_client.conversations_replies(
                channel=channel, ts=root, limit=200, cursor=cursor,
            )
            if not page.get("ok") or not isinstance(page.get("messages"), list):
                raise RuntimeError("Slack thread recovery failed")
            if page_index == 0 and not any(
                    isinstance(item, dict) and item.get("ts") == root
                    for item in page["messages"]):
                raise RuntimeError("Slack thread recovery lacks its root")
            for raw in page["messages"]:
                if not isinstance(raw, dict):
                    continue
                reply = {**raw, "type": "message", "channel": channel,
                         "thread_ts": None if raw.get("ts") == root else root}
                known_bots = {self.trusted_bot_id, *(self.settings.peer_bot_ids.values()
                                                     if self.settings else ())}
                if reply.get("bot_id") in known_bots and not reply.get("subtype"):
                    reply["subtype"] = "bot_message"
                normalized = normalize_event(reply, channel, self.trusted_bot_id,
                                             self.settings.peer_bot_ids if self.settings else None)
                if normalized is not None:
                    ts, message = normalized
                    self._append_message(reply, channel, ts, message)
            if not page.get("has_more"):
                if self.store.message_by_ts(channel, root) is None:
                    raise RuntimeError("Slack thread recovery lacks a durable root")
                return
            metadata = cast(dict[str, Any], page.get("response_metadata") or {})
            cursor = metadata.get("next_cursor")
            if not isinstance(cursor, str) or not cursor:
                raise RuntimeError("Slack thread recovery pagination is incomplete")
        raise RuntimeError("Slack thread recovery exceeded its bounded page count")
    def _answer_directory(self, client: BaseSocketModeClient, nonce: str) -> None:
        if self.settings is None or self.settings.local_prefix is None:
            return
        entries, complete = self.store.peers.local_entries(self.settings.local_prefix)
        try:
            client.web_client.chat_postMessage(
                channel=self.channel,
                text=wire({"kind": "snapshot", "nonce": nonce,
                           "entries": entries, "complete": complete}),
                parse="none", mrkdwn=False, unfurl_links=False, unfurl_media=False,
                blocks=[{"type": "context", "elements": [{"type": "plain_text",
                         "text": "AgentBus peer directory snapshot", "emoji": False}]}],
            )
        except Exception:
            LOGGER.error("Could not publish AgentBus peer directory snapshot")

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

            event = request.payload.get("event") if isinstance(request.payload, dict) else None
            raw_channel = event.get("channel") if isinstance(event, dict) else None
            channel: str = raw_channel if isinstance(raw_channel, str) else ""
            allowed = self.settings.allowed_channels if self.settings is not None else {self.channel}
            nested = event.get("message") if isinstance(event, dict) else None
            thread_update = (isinstance(event, dict) and event.get("subtype") == "message_replied") or (
                isinstance(event, dict) and event.get("hidden") is True
                and isinstance(nested, dict) and isinstance(nested.get("reply_count"), int)
                and nested["reply_count"] > 0
            )
            if (isinstance(event, dict) and channel in allowed
                    and event.get("type") == "message" and thread_update):
                try:
                    self._recover_thread(client, event)
                except Exception:
                    LOGGER.error("Slack thread recovery failed; withholding event acknowledgement", exc_info=True)
                    return
            peer_event = (parse_wire(event, self.channel, self.settings.peer_bot_ids)
                          if isinstance(event, dict) and self.settings else None)
            if peer_event is not None:
                bot, prefix, payload = peer_event
                try:
                    if payload["kind"] == "request":
                        if self.store.peers.claim_request("peer:" + bot, payload["nonce"]):
                            self._commands.submit(self._answer_directory, client, payload["nonce"])
                        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
                        return
                    if payload["kind"] == "snapshot" and self.store.peers.apply_snapshot(bot, prefix, payload):
                        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
                        return
                except sqlite3.Error:
                    LOGGER.error("Could not persist peer directory event; withholding acknowledgement")
                    return
            normalized = (
                normalize_event(event, channel, self.trusted_bot_id,
                                self.settings.peer_bot_ids if self.settings else None)
                if isinstance(event, dict) and channel in allowed else None
            )
            if normalized is not None and isinstance(event, dict):
                ts, message = normalized
                try:
                    self._append_message(event, channel, ts, message)
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
