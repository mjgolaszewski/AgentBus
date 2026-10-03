"""HTTP contract adapters for the participation and operator policy plane."""

from __future__ import annotations

import hmac
from typing import Annotated, Literal

from fastapi import Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

UPGRADE_NOTICE = (
    "Your AgentBus client does not advertise participation-v1. Update it from "
    "https://github.com/mjgolaszewski/AgentBus, then ask the operator for a profile handoff. "
    "Run `agentbus join --identity REPO:NAME --handoff-file PRIVATE_FILE`, acknowledge the "
    "delivered policy revision, and use `agentbus poll`. Legacy messaging still works, "
    "but this client cannot receive acknowledged stop controls."
)


def upgrade_notice(request: Request) -> str | None:
    return None if request.headers.get("X-AgentBus-Client-Capabilities") == "participation-v1" else UPGRADE_NOTICE


class SetPolicyRevision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["global", "repo", "chat"]
    scope_key: str = Field(min_length=1, max_length=80)
    values: dict[str, int | float | None]


class EnrollSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str = Field(min_length=1, max_length=64)
    route: str = Field(min_length=3, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    session_secret: str = Field(min_length=32, max_length=512, repr=False)
    handoff_token: str | None = Field(default=None, min_length=32, max_length=512, repr=False)


class IssueProfileHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str
    repo: str = Field(min_length=1, max_length=64)
    route: str = Field(min_length=3, max_length=80)


class IssueSessionRotation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str


class RotateSessionSecret(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rotation_token: str = Field(min_length=32, max_length=512, repr=False)
    new_session_secret: str = Field(min_length=32, max_length=512, repr=False)


class AcknowledgePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: str = Field(min_length=1, max_length=128)


class CheckInSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_backoff_seconds: float | None = Field(default=None, allow_inf_nan=False)


class StopControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    routes: list[str] | None = None
    session_ids: list[str] | None = None
    all_current: bool = False
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def require_one_target_mode(self) -> StopControl:
        if sum((self.all_current, bool(self.routes), bool(self.session_ids))) != 1:
            raise ValueError("choose routes, immutable session IDs, or all_current")
        return self


class AuxiliaryControl(StopControl):
    kind: Literal["nudge", "checkpoint_request", "temporary_policy_override", "pause_work", "resume_work"]
    values: dict[str, int | float | None] | None = None
    duration_seconds: int | None = Field(default=None, ge=1, le=86400)

    @model_validator(mode="after")
    def validate_override(self) -> AuxiliaryControl:
        if self.kind == "temporary_policy_override":
            if not self.values or self.duration_seconds is None:
                raise ValueError("temporary override requires values and duration_seconds")
        elif self.values is not None or self.duration_seconds is not None:
            raise ValueError("only temporary policy overrides accept values and duration")
        return self


ReportItem = Annotated[str, Field(min_length=1, max_length=500)]


class CheckpointReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    activity: str = Field(min_length=1, max_length=2000)
    blockers: list[ReportItem] = Field(default_factory=list, max_length=20)
    waiting_on: list[ReportItem] = Field(default_factory=list, max_length=20)
    work_refs: list[ReportItem] = Field(default_factory=list, max_length=20)


class AcknowledgeControl(BaseModel):
    model_config = ConfigDict(extra="forbid")

    report: CheckpointReport | None = None


class RenameSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: str = Field(min_length=3, max_length=80)


class RecoverClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_identity: str | None = Field(default=None, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
    reason: str = Field(min_length=1, max_length=1000)


def authenticate_operator(request: Request,
                          authorization: Annotated[str | None, Header()] = None) -> None:
    """A separate service-only capability owns policy and control mutations."""
    expected = request.app.state.settings.operator_token
    scheme, _, token = (authorization or "").partition(" ")
    if expected is None or scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(401, "Operator authorization required",
                            headers={"WWW-Authenticate": "Bearer"})


def api_policy_set(request: Request, body: SetPolicyRevision) -> dict:
    try:
        revision_id = request.app.state.store.participation.set_policy(
            scope=body.scope, scope_key=body.scope_key,
            values=body.values, actor="operator-capability",
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"revision_id": revision_id}


def api_policy_effective(request: Request, repo: str, chat_id: str) -> dict:
    try:
        policy = request.app.state.store.participation.effective_policy(repo, chat_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"revision": policy.revision, "values": policy.values, "sources": policy.sources}


def api_session_policy_explain(request: Request, session_id: str) -> dict:
    """Operator view joins policy provenance with read-only participation state."""
    try:
        policy = request.app.state.store.participation.effective_policy_for_session(session_id)
        presence = request.app.state.store.participation.presence(session_id)
    except KeyError:
        raise HTTPException(404, "Unknown participation session") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"revision": policy.revision, "values": policy.values,
            "sources": policy.sources, "presence": presence}


def api_session_enroll(request: Request, body: EnrollSession) -> dict:
    try:
        chat_id, session_id, revision = request.app.state.store.participation.enroll(
            repo=body.repo, route=body.route, display_name=body.display_name,
            session_secret=body.session_secret, handoff_token=body.handoff_token,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    policy = request.app.state.store.participation.effective_policy(body.repo, chat_id)
    return {"chat_id": chat_id, "session_id": session_id, "revision": revision,
            "values": policy.values, "sources": policy.sources, "ack_required": True}


def api_profile_handoff(request: Request, body: IssueProfileHandoff) -> dict:
    try:
        token = request.app.state.store.participation.issue_profile_handoff(
            chat_id=body.chat_id, repo=body.repo, route=body.route, actor="operator-capability",
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"handoff_token": token, "expires_in_seconds": 600}


def api_rotation_grant(request: Request, session_id: str, body: IssueSessionRotation) -> dict:
    try:
        token = request.app.state.store.participation.issue_session_rotation(
            chat_id=body.chat_id, session_id=session_id, actor="operator-capability",
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"rotation_token": token, "expires_in_seconds": 600}


def api_session_rotate_secret(request: Request, session_id: str, body: RotateSessionSecret) -> dict:
    try:
        receipt = request.app.state.store.participation.rotate_session_secret(
            session_id=session_id, token=body.rotation_token, new_secret=body.new_session_secret,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"receipt": receipt, "session_id": session_id}


def api_session_presence(request: Request, session_id: str) -> dict:
    try:
        return request.app.state.store.participation.presence(session_id)
    except KeyError:
        raise HTTPException(404, "Unknown participation session") from None


def api_session_self_presence(
    request: Request, session_id: str,
    session_token: Annotated[str | None, Header(alias="X-AgentBus-Session-Token")] = None,
) -> dict:
    try:
        return request.app.state.store.participation.self_presence(session_id, session_token or "")
    except PermissionError:
        raise HTTPException(401, "Session authorization required") from None


def api_session_roster(request: Request, include_stopped: bool = False) -> dict:
    return {"sessions": request.app.state.store.participation.roster(
        include_stopped=include_stopped)}


def api_session_policy_ack(
    request: Request, session_id: str, body: AcknowledgePolicy,
    session_token: Annotated[str | None, Header(alias="X-AgentBus-Session-Token")] = None,
) -> dict:
    try:
        receipt = request.app.state.store.participation.acknowledge_policy(
            session_id, session_token or "", body.revision,
        )
    except PermissionError:
        raise HTTPException(401, "Session authorization required") from None
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from None
    return {"receipt": receipt, "revision": body.revision}


def api_session_check_in(
    request: Request, session_id: str,
    body: CheckInSession | None = None,
    session_token: Annotated[str | None, Header(alias="X-AgentBus-Session-Token")] = None,
) -> dict:
    try:
        contacted = request.app.state.store.participation.contact(
            session_id, session_token or "",
            body.current_backoff_seconds if body else None,
        )
        controls = request.app.state.store.controls.deliver(session_id, session_token or "")
        if controls:
            return {"controls": controls, "last_client_contact_at": contacted}
        policy = request.app.state.store.participation.deliver_policy(session_id, session_token or "")
        state = request.app.state.store.participation.session_state(session_id)
    except PermissionError:
        raise HTTPException(401, "Session authorization required") from None
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from None
    return {"last_client_contact_at": contacted, "revision": policy.revision,
            "values": policy.values, "sources": policy.sources,
            "ack_required": state["acknowledged_policy_revision"] != policy.revision}


def api_control_stop(request: Request, body: StopControl) -> dict:
    try:
        control_id, targets = request.app.state.store.controls.issue_stop(
            routes=None if body.all_current else body.routes,
            session_ids=body.session_ids,
            reason=body.reason, actor="operator-capability",
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"control_id": control_id, "target_session_ids": targets}


def api_control_issue(request: Request, body: AuxiliaryControl) -> dict:
    try:
        control_id, targets = request.app.state.store.controls.issue_auxiliary(
            kind=body.kind, routes=None if body.all_current else body.routes,
            session_ids=body.session_ids,
            reason=body.reason, actor="operator-capability",
            override_values=body.values, duration_seconds=body.duration_seconds,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"control_id": control_id, "target_session_ids": targets, "kind": body.kind}


def api_control_status(request: Request, control_id: str) -> dict:
    return {"control_id": control_id, "targets": request.app.state.store.controls.status(control_id)}


def api_claim_recovery(request: Request, cursor: int, body: RecoverClaim) -> dict:
    try:
        return request.app.state.store.claim_recovery.reassign(
            channel=request.app.state.settings.slack_channel, cursor=cursor,
            new_identity=body.new_identity, actor="operator-capability", reason=body.reason,
        )
    except KeyError:
        raise HTTPException(404, "Message not found") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None


def api_control_ack(
    request: Request, control_id: str, session_id: str,
    body: AcknowledgeControl | None = None,
    session_token: Annotated[str | None, Header(alias="X-AgentBus-Session-Token")] = None,
) -> dict:
    try:
        receipt, kind = request.app.state.store.controls.acknowledge_control(
            control_id, session_id, session_token or "",
            report=body.report.model_dump() if body and body.report else None,
        )
    except PermissionError:
        raise HTTPException(401, "Session authorization required") from None
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from None
    return {"receipt": receipt, "control_id": control_id, "session_id": session_id,
            "state": "effective", "kind": kind,
            "directive": "STOP" if kind == "stop_end_turn" else "CONTINUE"}


def api_session_rename(
    request: Request, session_id: str, body: RenameSession,
    session_token: Annotated[str | None, Header(alias="X-AgentBus-Session-Token")] = None,
) -> dict:
    try:
        chat_id, old_route, new_route = request.app.state.store.participation.rename(
            session_id, session_token or "", body.route,
        )
    except PermissionError:
        raise HTTPException(401, "Session authorization required") from None
    except (ValueError, KeyError) as exc:
        raise HTTPException(409, str(exc)) from None
    return {"chat_id": chat_id, "session_id": session_id,
            "old_route": old_route, "new_route": new_route}
