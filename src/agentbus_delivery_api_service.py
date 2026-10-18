"""Version negotiation for the existing send endpoint; session checks precede custody."""
from __future__ import annotations

import asyncio
import sqlite3

from fastapi import HTTPException

from src.agentbus_channel_api_service import require_target, resolve_principal, serialize_message
from src.agentbus_federation_service import CustodyCapacityError
from src.agentbus_local_delivery_service import LocalDeliveryRejected, fingerprint, request_identity
from src.agentbus_message_lifecycle_service import LifecycleRejected
from src.agentbus_participation_store_service import StoppedMessageSession
from src.agentbus_send_policy_service import SessionAuthorityError, validate_new_send
from src.agentbus_send_rate_service import SendRateExceeded


async def send_message(request, message, session_token, response):
    store = request.app.state.store
    version = request.headers.get('X-AgentBus-Delivery-Version')
    if version not in {None, '1', '2'}:
        raise HTTPException(422, 'Unsupported local delivery version')
    principal_scope = resolve_principal(request, message.sender, session_token)
    if store.lifecycle.enabled and version not in {'1', '2'}:
        raise HTTPException(426, 'Lifecycle custody requires versioned delivery; immediate posting is disabled')
    if version == '2' and not store.lifecycle.enabled:
        raise HTTPException(426, 'Message lifecycle delivery is not enabled')
    channel = principal_scope.channel
    bridge = getattr(request.app.state, 'federation_bridge', None)
    bridge = bridge if bridge is not None and bridge.channel == channel else None
    digest = fingerprint(message.model_dump(include=set(message._authored_fields)))
    if message.reply_to_cursor is not None:
        internal = store.channels.internal_cursor(channel, message.reply_to_cursor)
        if internal is None:
            raise HTTPException(422, 'reply parent does not exist in this inbox')
        message.reply_to_cursor = internal
    try:
        principal = validate_new_send(store, channel, message, session_token)
        require_target(store, channel, message, bridge)
        if version in {'1', '2'}:
            if message._sender_assurance != 'session':
                raise SessionAuthorityError('Durable acceptance requires a matching joined session')
            key = request_identity(request.headers.get('Idempotency-Key'))
            accepted = store.local_delivery.accept(channel, key, digest, message, principal,
                bridge=bridge, epoch=request.headers.get('X-AgentBus-Request-Epoch') if version == '2' else None,
                session_token=session_token)
            response.status_code = 202
            response.headers['X-AgentBus-Delivery-Version'] = version
            response.headers['Idempotency-Key'] = key
            request.app.state.delivery.wake.set()
            return serialize_message(store, channel, accepted)
        store.send_rate.reserve(principal)
    except CustodyCapacityError:
        raise HTTPException(503, 'Federation custody capacity requires operator archival',
                            headers={'Retry-After': '60'}) from None
    except KeyError:
        raise HTTPException(422, 'reply parent does not exist in this inbox') from None
    except StoppedMessageSession as exc:
        raise HTTPException(423, str(exc)) from None
    except SendRateExceeded as exc:
        raise HTTPException(429, str(exc), headers={'Retry-After': str(exc.retry_after)}) from None
    except SessionAuthorityError as exc:
        raise HTTPException(403, str(exc)) from None
    except (LocalDeliveryRejected, LifecycleRejected) as exc:
        if isinstance(exc, LifecycleRejected):
            store.lifecycle.observe_clock()
        raise HTTPException(exc.status, str(exc),
                            headers={'Retry-After': '60'} if exc.status == 503 else None) from None
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, 'Local custody could not commit; no provider submission attempted') from None
    ts = await request.app.state.posters[channel].post(message)
    try:
        result = await asyncio.to_thread(store.append, channel, ts, message)
        return serialize_message(store, channel, result)
    except (sqlite3.Error, OSError):
        raise HTTPException(503, 'Message posted to Slack but local persistence failed; do not resend blindly') from None
