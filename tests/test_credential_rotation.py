"""Compromised participation credentials can be replaced without identity loss."""

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from src import agentbus_client, agentbus_rotation_client
from src.agentbus_parser_client import parse_args
from src.agentbus_participation_store_service import ParticipationStore
from src.agentbus_transport_client import ClientError

POLICY = {
    "initial_interval_seconds": 60, "backoff_factor": 2,
    "max_interval_seconds": 1920, "control_check_max_seconds": 60,
    "jitter_fraction": 0, "overdue_grace_seconds": 120,
    "presentation_budget_bytes": None,
}


def _open(path: Path) -> tuple[sqlite3.Connection, ParticipationStore]:
    db = sqlite3.connect(path)
    return db, ParticipationStore(db, threading.RLock())


def test_rotation_revokes_old_secret_and_preserves_session_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "inbox.sqlite3"
    db, store = _open(path)
    store.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    old = "old-session-secret-with-at-least-32-characters"
    new = "new-session-secret-with-at-least-32-characters"
    chat_id, session_id, revision = store.enroll(
        repo="agentbus", route="agentbus:flower", display_name="Flower", session_secret=old,
    )
    store.acknowledge_policy(session_id, old, revision)
    grant = store.issue_session_rotation(chat_id=chat_id, session_id=session_id, actor="operator")
    with pytest.raises(ValueError, match="unexpired rotation grant"):
        store.issue_session_rotation(chat_id=chat_id, session_id=session_id, actor="operator")
    with pytest.raises(ValueError, match="not valid"):
        store.rotate_session_secret(session_id=session_id, token="wrong" * 8, new_secret=new)
    assert store.contact(session_id, old)
    db.close()

    db, store = _open(path)
    receipt = store.rotate_session_secret(session_id=session_id, token=grant, new_secret=new)
    assert store.rotate_session_secret(session_id=session_id, token=grant, new_secret=new) == receipt
    with pytest.raises(PermissionError, match="session authority"):
        store.contact(session_id, old)
    assert store.contact(session_id, new)
    state = store.session_state(session_id)
    assert state["chat_id"] == chat_id
    assert state["acknowledged_policy_revision"] == revision
    assert state["state"] == "active"
    db.close()

    db, store = _open(path)
    assert store.rotate_session_secret(session_id=session_id, token=grant, new_secret=new) == receipt
    with pytest.raises(ValueError, match="already consumed"):
        store.rotate_session_secret(session_id=session_id, token=grant, new_secret="third-secret-with-at-least-32-characters")
    audit = [row[0] for row in db.execute("SELECT action FROM participation_audit ORDER BY audit_id")]
    assert audit.count("session_rotation_grant") == 1
    assert audit.count("session_secret_rotation") == 1
    db.close()


def test_persona_json_is_explicitly_public(monkeypatch, capsys) -> None:
    profile = {
        "schema_version": 2, "chat_id": "chat-id", "identity": "agentbus:flower",
        "repo": "agentbus", "name": "flower", "display_name": "Flower", "role": "gardener",
        "voice": "warm", "remit": "coordinate", "values": "care",
        "working_style": "steady", "signature": "a flower", "persona_file": None,
        "participation": {"session_id": "session-id", "session_secret": "never-print-this-session-secret"},
        "participation_pending": {"handoff_token": "never-print-this-handoff-token"},
        "participation_rotation_pending": {"rotation_token": "never-print-this-rotation-token",
                                           "new_session_secret": "never-print-this-new-secret"},
    }
    monkeypatch.setattr(agentbus_client, "checked_profile", lambda values, identity: (profile, {}))
    args = parse_args(["persona", "--identity", "agentbus:flower", "--json"])
    assert agentbus_client.cli_persona(args, Path("."), {}) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["display_name"] == "Flower"
    for secret in ("session_secret", "handoff_token", "rotation_token", "never-print-this"):
        assert secret not in output


def test_client_stages_new_credential_before_uncertain_response(tmp_path, monkeypatch, capsys) -> None:
    grant = tmp_path / "grant"
    grant.write_text("private-rotation-grant-with-at-least-32-characters\n")
    grant.chmod(0o600)
    profile = {"chat_id": "chat-id", "identity": "agentbus:flower",
               "participation": {"session_id": "session-id", "session_secret": "old-secret"}}
    monkeypatch.setattr(agentbus_rotation_client, "checked_profile", lambda values, identity: (profile, {}))
    monkeypatch.setattr(agentbus_rotation_client, "load_profile", lambda *args, **kwargs: profile)
    monkeypatch.setattr(agentbus_rotation_client, "save_profile", lambda values, data: None)
    seen = []

    def uncertain_then_receipt(values, path, body):
        seen.append((path, body.copy()))
        if len(seen) == 1:
            raise ClientError("response lost after commit")
        return {"session_id": "session-id", "receipt": "receipt-1"}

    monkeypatch.setattr(agentbus_rotation_client, "api", uncertain_then_receipt)
    with pytest.raises(ClientError, match="response lost"):
        agentbus_rotation_client.rotate_secret("agentbus:flower", str(grant), False, {})
    staged = profile["participation_rotation_pending"]
    assert staged["new_session_secret"] != "old-secret"
    assert agentbus_rotation_client.rotate_secret("agentbus:flower", None, False, {}) == 0
    assert seen[0] == seen[1]
    assert profile["participation"]["session_secret"] == staged["new_session_secret"]
    assert "participation_rotation_pending" not in profile
    assert "receipt-1" in capsys.readouterr().out


@pytest.mark.parametrize("stopped", [False, True])
def test_client_recovers_committed_rotation_after_old_secret_is_revoked(tmp_path, monkeypatch, stopped):
    db, store = _open(tmp_path / 'rotation.sqlite3')
    store.set_policy(scope='global', scope_key='*', values=POLICY, actor='operator')
    old = 'old-test-rotation-credential'.ljust(40, 'x')
    chat, session, revision = store.enroll(repo='agentbus', route='agentbus:flower',
        display_name='Flower', session_secret=old)
    store.acknowledge_policy(session, old, revision)
    token = store.issue_session_rotation(chat_id=chat, session_id=session, actor='operator')
    grant = tmp_path / 'grant'
    grant.write_text(token)
    grant.chmod(0o600)
    profiles = tmp_path / 'profiles'
    profiles.mkdir()
    values = {'AGENTBUS_CONSUMER_STATE_DIR': str(profiles), 'AGENTBUS_URL': 'http://127.0.0.1:8766'}
    profile = {'schema_version': 2, 'identity': 'agentbus:flower', 'chat_id': chat,
        'service_url': values['AGENTBUS_URL'], 'channel': 'C123', 'inbox_id': 'rotation-inbox',
        'ack_cursor': 8, 'observed_cursor': 10,
        'participation': {'session_id': session, 'session_secret': old}}
    path = profiles / 'agentbus:flower.json'
    path.write_text(json.dumps(profile))
    rotations = []

    def transport(_values, route, body=None, **kwargs):
        if route == '/v1/info':
            try:
                store.authorize_session(session, kwargs.get('session_token'))
            except PermissionError:
                raise ClientError('Old credential revoked', status=403) from None
            return {'chat_id': chat, 'session_id': session, 'channel': 'C123',
                'inbox_id': 'rotation-inbox', 'features': ['session-required', 'channel-isolation-v1',
                                                         'channel-local-cursors-v1']}
        assert route == f'/v1/sessions/{session}/rotate-secret'
        receipt = store.rotate_session_secret(session_id=session, token=body['rotation_token'],
                                             new_secret=body['new_session_secret'])
        rotations.append((dict(body), receipt))
        if len(rotations) == 1:
            raise ClientError('response lost after actual service commit')
        return {'session_id': session, 'receipt': receipt}

    monkeypatch.setattr(agentbus_client, 'api', transport)
    monkeypatch.setattr(agentbus_rotation_client, 'api', transport)
    try:
        with pytest.raises(ClientError, match='response lost'):
            agentbus_rotation_client.rotate_secret('agentbus:flower', str(grant), False, values)
        staged = json.loads(path.read_text())['participation_rotation_pending']
        if stopped:
            stopped_profile = agentbus_client.load_profile(values, 'agentbus:flower')
            stopped_profile['participation']['stopped'] = True
            agentbus_client.save_profile(values, stopped_profile)
        before = path.read_bytes()
        with pytest.raises(ClientError, match='service differs'):
            agentbus_rotation_client.rotate_secret('agentbus:flower', None, False,
                {**values, 'AGENTBUS_URL': 'http://127.0.0.1:9999'})
        assert path.read_bytes() == before and len(rotations) == 1
        with pytest.raises(PermissionError):
            store.authorize_session(session, old)
        assert agentbus_rotation_client.rotate_secret('agentbus:flower', None, False, values) == 0
        assert rotations[0] == rotations[1]
        current = json.loads(path.read_text())
        assert current['participation']['session_secret'] == staged['new_session_secret']
        assert 'participation_rotation_pending' not in current
        assert current['ack_cursor'] == 8 and current['observed_cursor'] == 10
        assert bool(current['participation'].get('stopped')) == stopped
    finally:
        db.close()
