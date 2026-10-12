"""Authenticated read and explicit refresh of the public peer directory."""

from __future__ import annotations

import re
import uuid
from typing import Annotated

import httpx
from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, Field

from src.agentbus_message_authorization_service import require_message_route
from src.agentbus_peer_directory_service import wire


class PeerRefresh(BaseModel):
    identity: str = Field(min_length=3, max_length=80)


def api_peers(request: Request) -> dict:
    settings = request.app.state.settings
    return dict(request.app.state.store.peers.list(settings.local_prefix, settings.peer_bot_ids),
                local_bot_id=request.app.state.local_bot_id)


async def api_peer_refresh(
    request: Request, body: PeerRefresh,
    x_agentbus_session_token: Annotated[str | None, Header()] = None,
) -> dict:
    settings = request.app.state.settings
    require_message_route(request.app.state.store, body.identity, x_agentbus_session_token)
    if not settings.peer_bot_ids or not settings.local_prefix:
        raise HTTPException(409, "No peer bots are configured for this service")
    nonce = str(uuid.uuid4())
    if not request.app.state.store.peers.claim_request("local", nonce):
        raise HTTPException(429, "Peer directory refresh is cooling down",
                            headers={"Retry-After": "30"})
    await post_directory(request.app.state.poster, wire({"kind": "request", "nonce": nonce}))
    return {"nonce": nonce, "status": "requested"}


async def post_directory(poster, text: str) -> None:
    """Keep directory control traffic outside the ordinary message/wake log."""
    try:
        response = await poster.client.post(
            "https://slack.com/api/chat.postMessage",
            json={"channel": poster.settings.slack_channel, "text": text, "parse": "none",
                  "mrkdwn": False, "unfurl_links": False, "unfurl_media": False,
                  "blocks": [{"type": "context", "elements": [{"type": "plain_text",
                              "text": "AgentBus peer directory exchange", "emoji": False}]}]},
            headers={"Authorization": f"Bearer {poster.settings.slack_bot_token}"},
        )
        data = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Peer refresh delivery is uncertain; inspect the channel before retrying") from None
    if (not response.is_success or not isinstance(data, dict) or data.get("ok") is not True
            or data.get("channel") != poster.settings.slack_channel
            or not isinstance(data.get("ts"), str)
            or not re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,20}", data["ts"])):
        raise HTTPException(502, "Peer refresh delivery is uncertain; inspect the channel before retrying")
