"""Secret-bound enrollment requests; only a verified operator chooses membership."""
from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
import uuid

from src.agentbus_channel_grants_service import validate_subject
from src.agentbus_message_assurance_service import route_parts
from src.agentbus_transaction_service import atomic

LIFETIME_SECONDS = 600
MAX_REQUESTS = 8192


class EnrollmentRequestExpired(PermissionError):
    """Expiry disclosed only after authenticating the retained request subject."""


def initialize(channels):
    channels.owner._db.execute('''CREATE TABLE IF NOT EXISTS channel_enrollment_requests (
        request_id TEXT PRIMARY KEY, repo TEXT NOT NULL, route TEXT NOT NULL,
        display_name TEXT NOT NULL, chat_id TEXT NOT NULL,
        secret_sha256 TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL,
        approved_channel TEXT REFERENCES bus_channels(channel), approved_actor TEXT,
        consumed_session TEXT REFERENCES participation_sessions(session_id))''')


def request_enrollment(channels, *, request_id, repo, route, display_name, session_secret, chat_id):
    """Stage without authority, or redeem the exact human-approved request atomically."""
    parts = route_parts(route)
    if (parts is None or parts[1] != repo or not 1 <= len(display_name.strip()) <= 120
            or not 32 <= len(session_secret) <= 512):
        raise PermissionError('Enrollment request unavailable')
    try:
        if str(uuid.UUID(request_id)) != request_id or str(uuid.UUID(chat_id)) != chat_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PermissionError('Enrollment request unavailable') from None
    store = channels.owner
    digest = hashlib.sha256(session_secret.encode()).hexdigest()
    try:
        with store._lock, atomic(store._db):
            row = store._db.execute('SELECT * FROM channel_enrollment_requests WHERE request_id=?', (request_id,)).fetchone()
            if row is None:
                store._db.execute('DELETE FROM channel_enrollment_requests WHERE consumed_session IS NULL AND expires_at<=?', (time.time(),))
                if store._db.execute('SELECT count(*) FROM channel_enrollment_requests').fetchone()[0] >= MAX_REQUESTS:
                    raise ValueError('Enrollment request capacity exhausted')
                if store._db.execute('SELECT 1 FROM participation_sessions WHERE secret_sha256=?', (digest,)).fetchone():
                    raise PermissionError('Enrollment request unavailable')
                store.send_rate._reserve('enrollment-requests')
                store._db.execute('INSERT INTO channel_enrollment_requests VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)',
                                  (request_id, repo, route, display_name, chat_id, digest, time.time() + LIFETIME_SECONDS))
                return None
            if ((row['repo'], row['route'], row['display_name'], row['chat_id']) != (repo, route, display_name, chat_id)
                    or not hmac.compare_digest(row['secret_sha256'], digest)):
                raise PermissionError('Enrollment request unavailable')
            if row['consumed_session'] is not None:
                session = store.participation.authorize_session(row['consumed_session'], session_secret)
                if (session['state'] == 'stopped' or row['approved_channel'] not in channels.enabled
                        or session['chat_id'] != chat_id or channels.channel_for_chat(chat_id) != row['approved_channel']):
                    raise PermissionError('Enrollment request unavailable')
                policy = store.participation.deliver_policy(session['session_id'], session_secret)
                return session['chat_id'], session['session_id'], policy.revision
            if row['expires_at'] <= time.time():
                raise EnrollmentRequestExpired('Enrollment request expired')
            if row['approved_channel'] is None:
                return None
            if row['approved_channel'] not in channels.enabled:
                raise PermissionError('Enrollment request unavailable')
            # The raw grant exists only within this transaction, never in Slack or request state.
            token = channels.issue_grant(channel=row['approved_channel'], repo=repo, route=route,
                                         chat_id=chat_id, actor=row['approved_actor'])
            result = channels.enroll(token, repo=repo, route=route, display_name=display_name, session_secret=session_secret)
            store._db.execute('UPDATE channel_enrollment_requests SET consumed_session=? WHERE request_id=?',
                              (result[1], request_id))
            return result
    except sqlite3.IntegrityError:
        raise PermissionError('Enrollment request unavailable') from None


def approve_enrollment(channels, request_id, *, channel, actor):
    """The invoking operator channel is the only source of membership authority."""
    if channel not in channels.enabled or not actor:
        raise PermissionError('Enrollment request unavailable')
    store = channels.owner
    with store._lock, atomic(store._db):
        row = store._db.execute('SELECT * FROM channel_enrollment_requests WHERE request_id=?', (request_id,)).fetchone()
        if row is None:
            raise PermissionError('Enrollment request unavailable')
        if row['approved_channel'] is not None:
            if row['approved_channel'] != channel:
                raise PermissionError('Enrollment request unavailable')
            if row['consumed_session'] is not None:
                return
        if row['expires_at'] <= time.time():
            raise PermissionError('Enrollment request expired or unavailable')
        validate_subject(channels, channel=channel, repo=row['repo'], route=row['route'], chat_id=row['chat_id'])
        if row['approved_channel'] is None:
            store._db.execute('UPDATE channel_enrollment_requests SET approved_channel=?,approved_actor=? WHERE request_id=?',
                              (channel, actor, request_id))


def pending_enrollments(channels, *, channel=None):
    """Privileged query deliberately excludes staged credential digests and grants."""
    with channels.owner._lock:
        rows = channels.owner._db.execute('''SELECT request_id,repo,route,display_name,chat_id,expires_at,approved_channel
            FROM channel_enrollment_requests WHERE consumed_session IS NULL AND expires_at>?
            AND (? IS NULL OR approved_channel IS NULL OR approved_channel=?)
            ORDER BY expires_at,request_id LIMIT 20''', (time.time(), channel, channel)).fetchall()
        return [dict(row) for row in rows]


def enroll_authorized(channels, *, repo, route, display_name, session_secret,
                      enrollment_token=None, handoff_token=None,
                      approval_request_id=None, profile_chat_id=None):
    """Application owner chooses exactly one existing enrollment authority."""
    if approval_request_id is not None:
        if enrollment_token or handoff_token or profile_chat_id is None:
            raise PermissionError('Choose one enrollment authorization')
        return channels.request_enrollment(request_id=approval_request_id, chat_id=profile_chat_id,
            repo=repo, route=route, display_name=display_name, session_secret=session_secret)
    if profile_chat_id is not None or (enrollment_token and handoff_token):
        raise PermissionError('Choose one enrollment authorization')
    return channels.enroll(enrollment_token or handoff_token, repo=repo, route=route,
                           display_name=display_name, session_secret=session_secret)
