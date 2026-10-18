"""Operator grants are consumed with identity creation in the existing writer."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import time
import uuid

from src.agentbus_message_assurance_service import route_parts
from src.agentbus_transaction_service import atomic


def validate_subject(channels, *, channel, repo, route, chat_id=None):
    """One owner validates both file grants and human approval subjects."""
    parts = route_parts(route)
    if channel not in channels.enabled or parts is None or parts[1] != repo:
        raise ValueError('Invalid enrollment grant scope')
    if chat_id is not None and str(uuid.UUID(chat_id)) != chat_id:
        raise ValueError('Invalid profile UUID')
    store = channels.owner
    if store._db.execute('SELECT 1 FROM chat_identities WHERE current_route=? OR chat_id=?', (route, chat_id)).fetchone() or store._db.execute(
            'SELECT 1 FROM routing_aliases WHERE route=?', (route,)).fetchone():
        raise ValueError('Enrollment identity already reserved')


def issue_grant(channels, *, channel, repo, route, actor, chat_id=None):
    if not actor:
        raise ValueError('Invalid enrollment grant scope')
    store = channels.owner
    token = secrets.token_urlsafe(32)
    with store._lock, atomic(store._db):
        validate_subject(channels, channel=channel, repo=repo, route=route, chat_id=chat_id)
        store._db.execute('DELETE FROM channel_enrollment_grants WHERE consumed_session IS NULL AND expires_at<?', (time.time(),))
        if store._db.execute('SELECT count(*) FROM channel_enrollment_grants').fetchone()[0] >= 8192:
            raise ValueError('Enrollment grant capacity exhausted')
        store._db.execute('INSERT INTO channel_enrollment_grants VALUES (?,?,?,?,?,?,?,NULL,NULL)',
            (hashlib.sha256(token.encode()).hexdigest(), channel, repo, route, chat_id, time.time()+600, actor))
    return token


def enroll(channels, token, *, repo, route, display_name, session_secret):
    if not isinstance(token,str) or not 32<=len(token)<=512 or not 32<=len(session_secret)<=512:
        raise PermissionError('Matching enrollment grant required')
    store=channels.owner
    digest=hashlib.sha256(session_secret.encode()).hexdigest()
    try:
        with store._lock, atomic(store._db):
            grant=store._db.execute('SELECT * FROM channel_enrollment_grants WHERE token_hash=?',
                (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if (grant is None or grant['channel'] not in channels.enabled or grant['repo']!=repo or grant['route']!=route):
                raise PermissionError('Matching enrollment grant required')
            if grant['consumed_session'] is not None:
                row=store._db.execute('SELECT * FROM participation_sessions WHERE session_id=?', (grant['consumed_session'],)).fetchone()
                if (row is None or row['state']=='stopped' or not hmac.compare_digest(row['secret_sha256'],digest)
                        or (grant['consumed_secret'] is not None and not hmac.compare_digest(grant['consumed_secret'],digest))):
                    raise PermissionError('Matching enrollment grant required')
                policy=store.participation.deliver_policy(row['session_id'],session_secret)
                return row['chat_id'],row['session_id'],policy.revision
            if grant['expires_at']<=time.time():
                raise PermissionError('Matching enrollment grant required')
            if store._db.execute('SELECT 1 FROM participation_sessions WHERE secret_sha256=?', (digest,)).fetchone():
                raise PermissionError('Session credential is already reserved')
            result=store.participation.enroll(repo=repo,route=route,display_name=display_name,
                session_secret=session_secret,granted_chat_id=grant['chat_id'],channel=grant['channel'])
            channels.bind(result[0],grant['channel'])
            store._db.execute('UPDATE channel_enrollment_grants SET consumed_session=?,consumed_secret=? WHERE token_hash=?',
                (result[1],digest,grant['token_hash']))
            return result
    except sqlite3.IntegrityError:
        raise PermissionError('Enrollment identity or credential already reserved') from None
