"""Explicit client-publication policy and custody; ordinary messaging has no edge here."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import secrets
import time
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from src.agentbus_message_assurance_service import route_parts
from src.agentbus_transaction_service import atomic

NAME = r"^[a-z][a-z0-9_-]{0,63}$"
DENIAL = "Publication destination is unavailable"
MAX_PUBLICATIONS = 1024
DRAFT_LIFETIME_SECONDS = 86400


class PublicationCapacityError(ValueError):
    """No new publication custody was admitted at the bounded row limit."""


class PublicationInstallation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str = Field(pattern=NAME)
    team_id: str = Field(pattern=r"^T[A-Z0-9]{2,79}$")
    bot_id: str = Field(pattern=r"^B[A-Z0-9]{2,79}$")
    token_env: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")


class PublicationDestination(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    name: str = Field(pattern=NAME)
    installation: str = Field(pattern=NAME)
    channel: str = Field(pattern=r"^[CG][A-Z0-9]{2,79}$")
    source_channels: list[str] = Field(min_length=1, max_length=16)
    authors: list[str] = Field(min_length=1, max_length=256)
    approvers: list[str] = Field(min_length=1, max_length=128)
    direct_authors: list[str] = Field(default_factory=list, max_length=256)
    enabled: bool = True
    label: str = Field(default="Project update", min_length=1, max_length=80)


class PublicationPolicy:
    def __init__(self, config, *, channels, operators, environ=None):
        if (not isinstance(config, dict) or set(config) != {"version", "installations", "destinations"}
                or type(config["version"]) is not int or config["version"] != 1
                or not isinstance(config["installations"], list) or not 1 <= len(config["installations"]) <= 16
                or not isinstance(config["destinations"], list) or not 1 <= len(config["destinations"]) <= 16):
            raise ValueError("Invalid bounded publication configuration")
        self.installations: dict[str, PublicationInstallation] = {}
        self.destinations: dict[str, PublicationDestination] = {}
        self._tokens = {}
        environment = os.environ if environ is None else environ
        for raw in config["installations"]:
            item = PublicationInstallation(**raw)
            if item.name in self.installations:
                raise ValueError("Duplicate publication installation")
            token = environment.get(item.token_env, "")
            if not isinstance(token, str) or not re.fullmatch(r"xoxb-[!-~]{1,500}", token):
                raise ValueError("Explicit protected publication credential is unavailable")
            self.installations[item.name] = item
            self._tokens[item.name] = token
        for raw in config["destinations"]:
            destination = PublicationDestination(**raw)
            if (destination.name in self.destinations or destination.installation not in self.installations
                    or not set(destination.source_channels) <= set(channels)
                    or not set(destination.approvers) <= set(operators)
                    or not set(destination.direct_authors) <= set(destination.authors)
                    or any(route_parts(route) is None for route in destination.authors)
                    or any(not re.fullmatch(r"[UW][A-Z0-9]{2,79}", actor) for actor in destination.approvers)
                    or any(len(set(values)) != len(values) for values in
                           (destination.source_channels, destination.authors, destination.approvers, destination.direct_authors))):
                raise ValueError("Invalid exact publication destination scope")
            self.destinations[destination.name] = destination

    def token(self, installation):
        return self._tokens[installation]

    def revision(self, destination):
        item = self.destinations[destination]
        spec = {"destination": item.model_dump(), "installation": self.installations[item.installation].model_dump()}
        return hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class PublicationStore:
    def __init__(self, owner, policy, *, now=time.time):
        self.owner, self.policy, self.now = owner, policy, now
        self.coordination_verified = False
        with owner._lock, atomic(owner._db):
            owner._db.execute("""CREATE TABLE IF NOT EXISTS publication_requests (
                publication_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, chat_id TEXT NOT NULL, session_id TEXT NOT NULL,
                route TEXT NOT NULL, channel TEXT NOT NULL, inbox_id TEXT NOT NULL,
                destination TEXT NOT NULL, text TEXT NOT NULL, policy_revision TEXT NOT NULL,
                digest TEXT NOT NULL, state TEXT NOT NULL, approval_actor TEXT,
                provider_ref TEXT, failure TEXT, created REAL NOT NULL, updated REAL NOT NULL,
                retry_at REAL NOT NULL DEFAULT 0, destination_spec TEXT NOT NULL, payload TEXT NOT NULL,
                UNIQUE(chat_id,channel,inbox_id,request_id))""")
            owner._db.execute("CREATE INDEX IF NOT EXISTS publication_author ON publication_requests(chat_id,session_id,created)")
            owner._db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", ("publication_epoch_v1", secrets.token_hex(32)))
            # The sole service owns the database lease: a previous in-flight attempt has an unknown outcome.
            owner._db.execute("UPDATE publication_requests SET state=? WHERE state=?", ("uncertain", "submitted"))

    def bind_coordination(self, team_id):
        self.coordination_verified = False
        if not isinstance(team_id, str) or not re.fullmatch(r"T[A-Z0-9]{2,79}", team_id):
            raise ValueError("Coordination workspace verification is required for publication")
        with self.owner._lock, atomic(self.owner._db):
            prior = self.owner._db.execute("SELECT value FROM metadata WHERE key=?", ("publication_coordination_workspace_v1",)).fetchone()
            if prior is not None and prior[0] != team_id:
                raise ValueError("Coordination workspace differs from retained publication custody")
            for dest in self.policy.destinations.values():
                if self.policy.installations[dest.installation].team_id == team_id and dest.channel in self.owner.channels.enabled:
                    raise ValueError("Client destination must be separate from coordination channels")
            self.owner._db.execute("INSERT OR IGNORE INTO metadata VALUES (?,?)", ("publication_coordination_workspace_v1", team_id))
        self.coordination_verified = True

    def request_epoch(self, channel):
        with self.owner._lock:
            row = self.owner._db.execute("SELECT value FROM metadata WHERE key=?", ("publication_epoch_v1",)).fetchone()
            if row is None or not re.fullmatch(r"[0-9a-f]{64}", row[0]):
                raise ValueError("Publication epoch custody is invalid")
            scope = json.dumps([channel, self.owner.channels.inbox_id(channel)]).encode()
            return hmac.digest(bytes.fromhex(row[0]), scope, "sha256").hex()

    def validate_epoch(self, principal, epoch):
        if (not self._session(vars(principal), active=True) or not isinstance(epoch, str)
                or not hmac.compare_digest(self.request_epoch(principal.channel), epoch)):
            raise PermissionError("Publication request epoch or session scope differs; preserve the original request")

    def _session(self, scope, *, active):
        row = self.owner._db.execute("""SELECT s.state,s.chat_id,c.current_route,cc.channel FROM participation_sessions s
            JOIN chat_identities c USING(chat_id) JOIN chat_channels cc USING(chat_id) WHERE s.session_id=?""",
            (scope["session_id"],)).fetchone()
        return bool(row and row["chat_id"] == scope["chat_id"] and row["channel"] == scope["channel"]
                    and row["current_route"] == scope["route"]
                    and scope["channel"] in self.owner.channels.enabled
                    and self.owner.channels.inbox_id(scope["channel"]) == scope["inbox_id"]
                    and (not active or row["state"] in {"active", "overdue"}))

    def _destination(self, scope, name):
        dest = self.policy.destinations.get(name)
        if (not self._session(scope, active=True) or dest is None or not dest.enabled
                or scope["route"] not in dest.authors or scope["channel"] not in dest.source_channels):
            raise PermissionError(DENIAL)
        return dest

    def _receipt(self, row):
        return {key: row[key] for key in ("request_id", "publication_id", "destination", "text", "digest", "state",
                "approval_actor", "provider_ref", "failure", "created", "updated", "retry_at")}

    def _retained_payload(self, row):
        binding, payload = json.loads(row["destination_spec"]), json.loads(row["payload"])
        spec = [row["chat_id"], row["session_id"], row["channel"], row["inbox_id"], row["destination"],
                row["text"], row["policy_revision"], binding, payload, row["request_id"], row["route"]]
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if (not hmac.compare_digest(row["digest"], digest) or payload["client_msg_id"] != row["publication_id"]
                or payload["channel"] != binding["channel"]):
            raise ValueError("Retained publication payload differs from its immutable digest")
        return binding, payload

    def _operator_receipt(self, row):
        binding, payload = self._retained_payload(row)
        dest = self.policy.destinations.get(row["destination"])
        can_approve = bool(dest and dest.enabled and row["state"] == "awaiting_approval"
                           and row["policy_revision"] == self.policy.revision(dest.name)
                           and 0 <= self.now() - row["created"] < DRAFT_LIFETIME_SECONDS)
        return {**self._receipt(row), "author": row["route"], "policy_revision": row["policy_revision"],
                "destination_binding": binding, "payload_text": payload["text"], "payload_blocks": payload["blocks"],
                "can_approve": can_approve}

    def _row(self, request_id):
        return self.owner._db.execute("SELECT * FROM publication_requests WHERE publication_id=?", (request_id,)).fetchone()

    def admit(self, principal, request_id, destination, text):
        if (not isinstance(request_id, str) or str(UUID(request_id)) != request_id
                or not isinstance(text, str) or not text.strip() or len(text) > 6000):
            raise ValueError("A canonical request UUID and1..6000 text characters are required")
        with self.owner._lock, atomic(self.owner._db):
            dest = self._destination(vars(principal), destination)
            publication_id = str(uuid5(NAMESPACE_URL, json.dumps([principal.inbox_id, principal.chat_id,
                                                                 principal.session_id, request_id])))
            from src.agentbus_presentation_service import section_blocks, slack_escape
            installation = self.policy.installations[dest.installation]
            binding = {"installation": dest.installation, "team_id": installation.team_id,
                       "bot_id": installation.bot_id, "channel": dest.channel, "label": dest.label}
            original_render = "*" + dest.label + "*\n" + text
            rendered = slack_escape(original_render)
            payload = {"channel": dest.channel, "text": rendered, "blocks": section_blocks(original_render),
                       "client_msg_id": publication_id, "unfurl_links": False, "unfurl_media": False, "parse": "none"}
            spec = [principal.chat_id, principal.session_id, principal.channel, principal.inbox_id,
                    destination, text, self.policy.revision(destination), binding, payload, request_id, principal.route]
            digest = hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            old = self.owner._db.execute("SELECT * FROM publication_requests WHERE chat_id=? AND channel=? AND inbox_id=? AND request_id=?",
                (principal.chat_id, principal.channel, principal.inbox_id, request_id)).fetchone()
            if old is not None and old["session_id"] != principal.session_id:
                raise PermissionError(DENIAL)
            if old is not None:
                if old["request_id"] != request_id or old["digest"] != digest:
                    raise ValueError("Publication request conflicts with retained identity")
                return self._receipt(old)
            if self.owner._db.execute("SELECT count(*) FROM publication_requests").fetchone()[0] >= MAX_PUBLICATIONS:
                raise PublicationCapacityError("Publication custody capacity requires operator review")
            self.owner.lifecycle.protect_extensions()
            self.owner.retention.reserve_storage(len(text.encode("utf-8")) +
                len(json.dumps([binding, payload], ensure_ascii=False).encode("utf-8")) + 2048)
            self.owner.send_rate._reserve("session:" + principal.session_id)
            state = "ready" if principal.route in dest.direct_authors else "awaiting_approval"
            instant = self.now()
            self.owner._db.execute("INSERT INTO publication_requests(publication_id,request_id,chat_id,session_id,route,channel,inbox_id,destination,text,policy_revision,digest,state,approval_actor,provider_ref,failure,created,updated,destination_spec,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (publication_id, request_id, principal.chat_id, principal.session_id, principal.route, principal.channel,
                 principal.inbox_id, destination, text, spec[6], digest, state, None, None, None, instant, instant,
                 json.dumps(binding, sort_keys=True, ensure_ascii=False), json.dumps(payload, sort_keys=True, ensure_ascii=False)))
            return self._receipt(self._row(publication_id))

    def approve(self, request_id, actor, digest):
        with self.owner._lock, atomic(self.owner._db):
            row = self._row(request_id)
            dest = self.policy.destinations.get(row["destination"]) if row else None
            if dest is None or actor not in dest.approvers:
                raise PermissionError(DENIAL)
            self._destination(row, dest.name)
            self._retained_payload(row)
            if row["digest"] != digest or row["policy_revision"] != self.policy.revision(dest.name):
                raise ValueError("Approval does not match the immutable publication")
            if row["state"] != "awaiting_approval" and row["approval_actor"] == actor:
                return self._receipt(row)
            if row["state"] != "awaiting_approval" or self.now() - row["created"] >= DRAFT_LIFETIME_SECONDS:
                raise ValueError("Publication is no longer awaiting valid approval")
            self.owner._db.execute("UPDATE publication_requests SET state=?,approval_actor=?,updated=? WHERE publication_id=?",
                                   ("ready", actor, self.now(), request_id))
            return self._receipt(self._row(request_id))

    def status(self, principal, request_id):
        with self.owner._lock:
            if not self._session(vars(principal), active=False):
                raise PermissionError("Publication receipt is unavailable")
            row = self.owner._db.execute("SELECT * FROM publication_requests WHERE request_id=? AND chat_id=? AND session_id=? AND channel=? AND inbox_id=?",
                (request_id, principal.chat_id, principal.session_id, principal.channel, principal.inbox_id)).fetchone()
            if row is None:
                raise PermissionError("Publication receipt is unavailable")
            return self._receipt(row)

    def cancel(self, principal, request_id):
        with self.owner._lock, atomic(self.owner._db):
            receipt = self.status(principal, request_id)
            publication_id = receipt["publication_id"]
            row = self._row(publication_id)
            if row["state"] not in {"draft", "awaiting_approval", "ready", "cancelled"}:
                raise ValueError("Submission may have started; cancellation is unsupported")
            self.owner._db.execute("UPDATE publication_requests SET state=?,updated=? WHERE publication_id=?",
                                   ("cancelled", self.now(), publication_id))
            return self._receipt(self._row(publication_id))


    def prepare(self, request_id):
        with self.owner._lock, atomic(self.owner._db):
            row = self._row(request_id)
            if row is None or row["state"] != "ready":
                return None
            try:
                self._retained_payload(row)
                valid = True
            except (ValueError, KeyError, TypeError, RecursionError):
                valid = False
            if not valid:
                self.owner._db.execute("UPDATE publication_requests SET state=?,failure=?,updated=? WHERE publication_id=?",
                                       ("failed", "retained_payload_mismatch", self.now(), request_id))
                return None
            if not 0 <= self.now() - row["created"] < DRAFT_LIFETIME_SECONDS:
                self.owner._db.execute("UPDATE publication_requests SET state=?,failure=?,updated=? WHERE publication_id=?",
                                       ("failed", "expired", self.now(), request_id))
                return None
            try:
                self._destination(row, row["destination"])
                if row["policy_revision"] != self.policy.revision(row["destination"]):
                    raise PermissionError(DENIAL)
            except PermissionError:
                self.owner._db.execute("UPDATE publication_requests SET state=?,failure=?,updated=? WHERE publication_id=?",
                                       ("failed", "authorization_changed", self.now(), request_id))
                return None
            return dict(row)

    def begin(self, request_id):
        with self.owner._lock, atomic(self.owner._db):
            row = self.prepare(request_id)
            if row is None:
                return None
            self.owner._db.execute("UPDATE publication_requests SET state=?,failure=?,retry_at=0,updated=? WHERE publication_id=?",
                                   ("submitted", None, self.now(), request_id))
            return dict(self._row(request_id))

    def settle(self, request_id, state, *, provider_ref=None, failure=None, pause_provider=None):
        if state not in {"delivered", "failed", "uncertain"}:
            raise ValueError("Invalid publication outcome")
        with self.owner._lock, atomic(self.owner._db):
            row = self._row(request_id)
            if row is None or row["state"] not in {"submitted", "uncertain"}:
                raise ValueError("Publication is not an in-flight submission")
            if pause_provider is not None:
                self._pause_provider(*pause_provider, seconds=60)
            self.owner._db.execute("UPDATE publication_requests SET state=?,provider_ref=?,failure=?,updated=? WHERE publication_id=?",
                                   (state, provider_ref, failure, self.now(), request_id))
            return self._receipt(self._row(request_id))

    def list_author(self, principal, *, after=None, limit=20):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Publication page size must be1..20")
        with self.owner._lock:
            if not self._session(vars(principal), active=False):
                raise PermissionError("Publication receipt is unavailable")
            params = [principal.chat_id, principal.session_id, principal.channel, principal.inbox_id]
            tail = ""
            if after is not None:
                previous = self.status(principal, after)
                tail = " AND (created<? OR (created=? AND request_id<?))"
                params.extend([previous["created"], previous["created"], after])
            rows = self.owner._db.execute("SELECT * FROM publication_requests WHERE chat_id=? AND session_id=? AND channel=? AND inbox_id=?" + tail +
                " ORDER BY created DESC,request_id DESC LIMIT ?", [*params, limit + 1]).fetchall()
            return {"publications": [self._receipt(row) for row in rows[:limit]], "has_more": len(rows) > limit,
                    "next_after": rows[limit - 1]["request_id"] if len(rows) > limit else None}

    def _operator_destinations(self, actor):
        return [name for name, item in self.policy.destinations.items() if actor in item.approvers]

    def list_operator(self, actor, channel, *, page=0):
        if type(page) is not int or not 0 <= page <= 65535:
            raise ValueError("Invalid publication page")
        names = self._operator_destinations(actor)
        if not names:
            return {"publications": [], "page": 0, "pages": 1}
        with self.owner._lock:
            where = "channel=? AND destination IN (" + ",".join("?" for _ in names) + ")"
            count = self.owner._db.execute("SELECT count(*) FROM publication_requests WHERE " + where, [channel, *names]).fetchone()[0]
            pages = max(1, (count + 4) // 5)
            if page >= pages:
                raise ValueError("Choose an existing publication page")
            rows = self.owner._db.execute("SELECT * FROM publication_requests WHERE " + where +
                " ORDER BY created DESC,request_id DESC LIMIT 5 OFFSET ?", [channel, *names, page * 5]).fetchall()
            return {"publications": [self._operator_receipt(row) for row in rows], "page": page, "pages": pages}

    def operator_status(self, request_id, actor, channel):
        names = self._operator_destinations(actor)
        with self.owner._lock:
            row = self.owner._db.execute("SELECT * FROM publication_requests WHERE publication_id=? AND channel=? AND destination IN (" +
                ",".join("?" for _ in names) + ")", [request_id, channel, *names]).fetchone() if names else None
            if row is None:
                raise PermissionError(DENIAL)
            return self._operator_receipt(row)

    def next_ready(self):
        with self.owner._lock:
            row = self.owner._db.execute("SELECT publication_id FROM publication_requests WHERE state=? AND retry_at<=? ORDER BY updated,request_id LIMIT 1",
                                         ("ready", self.now())).fetchone()
            return row["publication_id"] if row else None

    def defer_ready(self, request_id, *, failure, seconds=60, provider=None):
        if failure not in {"provider_verification_unavailable", "provider_rate_limited"} or not 1 <= seconds <= 86400:
            raise ValueError("Invalid publication backoff")
        with self.owner._lock, atomic(self.owner._db):
            row = self._row(request_id)
            allowed = {"ready"} if failure == "provider_verification_unavailable" else {"ready", "submitted"}
            if row is None or row["state"] not in allowed:
                raise ValueError("Publication cannot enter this recovery state")
            if provider is not None:
                self._pause_provider(*provider, seconds=seconds)
            self.owner._db.execute("UPDATE publication_requests SET state=?,failure=?,retry_at=?,updated=? WHERE publication_id=?",
                                   ("ready", failure, self.now() + seconds, self.now(), request_id))

    def fail_before_submission(self, request_id, failure):
        if failure != "provider_binding_mismatch":
            raise ValueError("Invalid pre-submission failure")
        with self.owner._lock, atomic(self.owner._db):
            self.owner._db.execute("UPDATE publication_requests SET state=?,failure=?,updated=? WHERE publication_id=? AND state=?",
                                   ("failed", failure, self.now(), request_id, "ready"))

    def _backoffs(self):
        row = self.owner._db.execute("SELECT value FROM metadata WHERE key=?", ("publication_provider_backoff_v1",)).fetchone()
        values = json.loads(row[0]) if row else {}
        if (not isinstance(values, dict) or len(values) > 128 or any(
                not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key)
                or type(value) not in {int, float} or not math.isfinite(value) or value < 0
                for key, value in values.items())):
            raise ValueError("Publication provider backoff custody is invalid")
        return values

    def _provider_key(self, installation, method):
        if method not in {"auth.test", "chat.postMessage"}:
            raise ValueError("Unsupported publication provider method")
        return hashlib.sha256(json.dumps([installation.team_id, installation.bot_id, method]).encode()).hexdigest()

    def provider_retry_at(self, installation, method):
        with self.owner._lock:
            return self._backoffs().get(self._provider_key(installation, method), 0)

    def _pause_provider(self, installation, method, *, seconds):
        # Caller owns the writer transaction with the request outcome or deferral.
        key = self._provider_key(installation, method)
        values = {name: until for name, until in self._backoffs().items() if until > self.now()}
        if len(values) >= 128 and key not in values:
            raise ValueError("Publication provider cooldown capacity requires operator review")
        values[key] = max(values.get(key, 0), self.now() + seconds)
        self.owner._db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)",
                               ("publication_provider_backoff_v1", json.dumps(values, sort_keys=True)))


def load_publication_policy(settings):
    if settings.publication_config is None:
        return None
    with settings.publication_config.open("rb") as source:
        raw = source.read(262145)
    if len(raw) > 262144:
        raise ValueError("Publication configuration exceeds its byte bound")
    try:
        config = json.loads(raw)
        return PublicationPolicy(config, channels=settings.channels, operators=settings.slack_operator_user_ids)
    except (ValueError, TypeError, RecursionError):
        raise ValueError("Invalid publication configuration; verify local policy and credential references") from None
