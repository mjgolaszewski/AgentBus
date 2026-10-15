"""Private authenticated enqueue contract; no federation HTTP ingress."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.agentbus_federation_bridge_service import EnqueueRejected, enqueue_authenticated


class FederationSendRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    origin_message_id: str = Field(min_length=1, max_length=128)
    destination_instance_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
    message: dict[str, Any]


class FederationAcceptance(BaseModel):
    origin_instance_id: str
    origin_message_id: str
    destination_instance_id: str
    accepted: bool = True


def api_federation_enqueue(request: Request, body: FederationSendRequest,
        x_agentbus_session_token: Annotated[str | None, Header()] = None) -> FederationAcceptance:
    bridge = request.app.state.federation_bridge
    if bridge is None:
        raise HTTPException(503, 'Federation is not configured')
    try:
        enqueue_authenticated(bridge, request.app.state.store, request.app.state.settings.slack_channel,
                              body.origin_message_id, body.destination_instance_id, body.message, x_agentbus_session_token)
    except EnqueueRejected as exc:
        raise HTTPException(exc.status, str(exc), headers=exc.headers) from None
    return FederationAcceptance(origin_instance_id=bridge.identity.instance_id,
        origin_message_id=body.origin_message_id, destination_instance_id=body.destination_instance_id)
