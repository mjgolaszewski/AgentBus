"""Version negotiation for the existing send endpoint; session checks precede custody."""
from __future__ import annotations

import asyncio
import sqlite3

from fastapi import HTTPException

from src.agentbus_federation_service import CustodyCapacityError
from src.agentbus_local_delivery_service import LocalDeliveryRejected, fingerprint, request_identity
from src.agentbus_participation_store_service import StoppedMessageSession
from src.agentbus_send_policy_service import SessionAuthorityError, validate_new_send
from src.agentbus_send_rate_service import SendRateExceeded


async def send_message(request, message, session_token, response):
    settings, store = request.app.state.settings, request.app.state.store
    version = request.headers.get('X-AgentBus-Delivery-Version')
    if version not in {None, '1'}:
        raise HTTPException(422, 'Unsupported local delivery version')
    digest = fingerprint(message.model_dump(include=set(message._authored_fields)))
    try:
        principal = validate_new_send(store, settings.slack_channel, message, session_token)
        if version == '1':
            if message._sender_assurance != 'session':
                raise SessionAuthorityError('Durable acceptance requires a matching joined session')
            key = request_identity(request.headers.get('Idempotency-Key'))
            accepted = store.local_delivery.accept(settings.slack_channel, key, digest, message, principal,
                bridge=getattr(request.app.state, 'federation_bridge', None))
            response.status_code = 202
            response.headers['X-AgentBus-Delivery-Version'] = '1'
            response.headers['Idempotency-Key'] = key
            request.app.state.delivery.wake.set()
            return accepted
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
    except LocalDeliveryRejected as exc:
        raise HTTPException(exc.status, str(exc),
                            headers={'Retry-After': '60'} if exc.status == 503 else None) from None
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, 'Local custody could not commit; no provider submission attempted') from None
    ts = await request.app.state.poster.post(message)
    try:
        return await asyncio.to_thread(store.append, settings.slack_channel, ts, message)
    except (sqlite3.Error, OSError):
        raise HTTPException(503, 'Message posted to Slack but local persistence failed; do not resend blindly') from None
