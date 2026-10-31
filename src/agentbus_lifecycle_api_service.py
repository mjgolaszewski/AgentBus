"""Thin session-scoped pickup and completion receipts over the lifecycle owner."""
from __future__ import annotations

from typing import Annotated

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agentbus_channel_api_service import assert_session_scope, resolve_principal
from src.agentbus_message_lifecycle_service import LifecycleRejected

UUID_PATTERN = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'


class WorkReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(pattern=UUID_PATTERN)
    receipt_id: str = Field(pattern=UUID_PATTERN)


class WorkItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cursor: int = Field(ge=1)
    receipt_id: str = Field(pattern=UUID_PATTERN)


class HandledBatch(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str = Field(pattern=UUID_PATTERN)
    receipts: Annotated[list[WorkItem], Field(min_length=1, max_length=200)]


def decode_work_receipt(payload):
    try:
        return WorkReceipt(**payload)
    except ValidationError:
        raise HTTPException(422, 'Invalid work receipt shape') from None


def decode_handled_batch(payload):
    try:
        return HandledBatch(**payload)
    except ValidationError:
        raise HTTPException(422, 'Invalid handling receipt batch shape') from None


def _session_secret(request):
    return request.headers.get('X-AgentBus-Session-Token')


def _internal(store, channel, cursor):
    internal = store.channels.internal_cursor(channel, cursor)
    if internal is None and store.lifecycle.enabled and store.retention.has_retired_positions(channel, cursor-1, cursor):
        raise HTTPException(410, 'History retired in this inbox; no work or acknowledgement is inferred')
    if internal is None or not store.lifecycle.enabled:
        raise HTTPException(404, 'Recipient outcome unavailable in this scope')
    return internal


def _receipt_scope(request, session_id, secret):
    assert_session_scope(request, session_id, secret)
    try:
        return request.app.state.store.channels.principal(secret, receipt_recovery=True)
    except PermissionError:
        raise HTTPException(403, 'Matching session scope required') from None


def api_pickup(request: Request, cursor: int, payload: dict):
    receipt = decode_work_receipt(payload)
    principal = resolve_principal(request)
    if receipt.session_id != principal.session_id:
        raise HTTPException(403, 'Matching session scope required')
    store = request.app.state.store
    internal = _internal(store, principal.channel, cursor)
    try:
        result = store.lifecycle.pickup(principal, internal, receipt.receipt_id,
                                        session_secret=_session_secret(request))
    except PermissionError:
        raise HTTPException(403, 'Matching session scope required') from None
    except LifecycleRejected as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return {'cursor':cursor, 'receipt_id':receipt.receipt_id, 'state':result['state'],
            'lease_until':result['lease_until']}


def api_complete(request: Request, cursor: int, payload: dict):
    receipt = decode_work_receipt(payload)
    secret = _session_secret(request)
    principal = _receipt_scope(request, receipt.session_id, secret)
    store = request.app.state.store
    internal = _internal(store, principal.channel, cursor)
    try:
        result = store.lifecycle.finish(principal, internal, receipt.receipt_id, session_secret=secret)
    except PermissionError:
        raise HTTPException(403, 'Matching session scope required') from None
    except LifecycleRejected as exc:
        raise HTTPException(exc.status, str(exc)) from None
    return {'cursor':cursor, 'receipt_id':receipt.receipt_id, 'state':result['state'],
            'completed_at':result['outcome_at']}


def api_request_outcome(request: Request, request_id: str):
    principal = resolve_principal(request)
    try:
        return request.app.state.store.lifecycle.request_outcome(principal, request_id)
    except LifecycleRejected as exc:
        raise HTTPException(exc.status, str(exc)) from None


def api_outcome(request: Request, cursor: int):
    principal = resolve_principal(request)
    store = request.app.state.store
    internal = _internal(store, principal.channel, cursor)
    try:
        return {'cursor':cursor, **store.lifecycle.outcome(principal, internal)}
    except LifecycleRejected as exc:
        raise HTTPException(exc.status, str(exc)) from None


def api_handled_batch(request: Request, payload: dict):
    batch = decode_handled_batch(payload)
    secret = _session_secret(request)
    principal = _receipt_scope(request, batch.session_id, secret)
    store = request.app.state.store
    receipts = [(_internal(store, principal.channel, item.cursor), item.receipt_id) for item in batch.receipts]
    try:
        return {'session_id':batch.session_id, **store.lifecycle.handle_batch(principal, receipts,
                    session_secret=secret)}
    except PermissionError:
        raise HTTPException(403, 'Matching session scope required') from None
    except LifecycleRejected as exc:
        raise HTTPException(exc.status, str(exc)) from None
