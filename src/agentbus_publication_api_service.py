"""Session-scoped HTTP translation for explicit publication admission and receipts."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Annotated

from fastapi import HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.agentbus_channel_api_service import resolve_principal
from src.agentbus_local_delivery_service import request_identity
from src.agentbus_message_lifecycle_service import LifecycleRejected
from src.agentbus_publication_service import PublicationCapacityError
from src.agentbus_send_rate_service import SendRateExceeded


class PublicationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    destination: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    text: str = Field(min_length=1, max_length=6000)


def decode_publication_intent(value):
    try:
        return PublicationIntent(**value)
    except (ValidationError, TypeError):
        raise HTTPException(422, "Publication requires only a named destination and bounded text") from None


def publication_owner(request, *, receipt=False):
    principal = resolve_principal(request, receipt_recovery=receipt)
    owner = getattr(request.app.state, "publications", None)
    if owner is None:
        raise HTTPException(409, "Client publication is disabled or unsupported; no provider effect inferred")
    return principal, owner


@contextmanager
def publication_errors():
    """One HTTP error contract, reused by the distinct query and mutation handlers."""
    try:
        yield
    except PermissionError:
        raise HTTPException(403, "Publication request, receipt or destination is unavailable in this scope") from None
    except SendRateExceeded as exc:
        raise HTTPException(429, "Publication admission rate exceeded", headers={"Retry-After": str(exc.retry_after)}) from None
    except LifecycleRejected as exc:
        raise HTTPException(exc.status, "Publication storage admission is unavailable; preserve the original request") from None
    except PublicationCapacityError:
        raise HTTPException(503, "Publication custody capacity requires operator review; preserve the original request",
                            headers={"Retry-After": "60"}) from None
    except ValueError:
        raise HTTPException(409, "Publication request conflicts, expired or cannot enter the requested state; inspect the original receipt") from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Publication custody is unavailable; preserve the original request and inspect its receipt") from None


def create_publication(request, intent):
    principal, owner = publication_owner(request)
    intent = decode_publication_intent(intent)
    with publication_errors():
        owner.validate_epoch(principal, request.headers.get("X-AgentBus-Request-Epoch"))
        key = request_identity(request.headers.get("Idempotency-Key"))
        result = owner.admit(principal, key, intent.destination, intent.text)
    request.app.state.delivery.wake.set()
    return result


def read_publications(request, *, request_id=None, after=None, limit=20):
    principal, owner = publication_owner(request, receipt=True)
    with publication_errors():
        if request_id is not None:
            return owner.status(principal, request_id)
        return owner.list_author(principal, after=after, limit=limit)


def cancel_publication(request, request_id):
    principal, owner = publication_owner(request)
    with publication_errors():
        return owner.cancel(principal, request_id)


def api_publication_create(request: Request, intent: dict, response: Response):
    result = create_publication(request, intent)
    response.status_code = 202
    return result


def api_publication_list(request: Request, after: Annotated[str | None, Query(max_length=36)] = None,
                         limit: Annotated[int, Query(ge=1, le=20)] = 20):
    return read_publications(request, after=after, limit=limit)


def api_publication_status(request: Request, request_id: str):
    return read_publications(request, request_id=request_id)


def api_publication_cancel(request: Request, request_id: str):
    return cancel_publication(request, request_id)
