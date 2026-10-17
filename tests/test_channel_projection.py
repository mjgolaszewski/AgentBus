"""Foreign disposable projections cannot affect scoped cursors or wake attempts."""
import json
import uuid

import pytest

from src.agentbus_transport_client import ClientError


def profile():
    return {'identity':'home:agentbus:a','chat_id':str(uuid.uuid4()),'channel':'C111',
        'inbox_id':str(uuid.uuid4()),'service_url':'http://127.0.0.1:8766',
        'ack_cursor':0,'participation':{'session_id':str(uuid.uuid4()),'session_secret':'x'*40}}


@pytest.mark.parametrize('field,value', [('channel','C222'),('inbox_id','foreign-inbox'),
    ('service_url','http://127.0.0.1:9999')])
def test_foreign_wake_scan_is_rejected_without_rewriting_evidence(tmp_path, field, value):
    from src import agentbus_wake_inbox_client as wake
    values={'AGENTBUS_STATE_DIR':str(tmp_path)}
    p=profile()
    state=wake._fresh(p,1)
    state[field]=value
    path=wake.state_path(values,p['chat_id'])
    path.parent.mkdir(parents=True,exist_ok=True)
    original=json.dumps(state).encode()
    path.write_bytes(original)
    with pytest.raises(ClientError, match='scope|binding|channel|inbox'):
        wake.read_state(values,p,1)
    assert path.read_bytes() == original
    assert p['ack_cursor'] == 0


@pytest.mark.parametrize('field,value', [('channel','C222'),('inbox_id','foreign-inbox'),
    ('service_url','http://127.0.0.1:9999')])
def test_foreign_spool_cannot_advance_ack_or_merge_events(tmp_path,field,value):
    from src import agentbus_poll_client as poll
    p=profile()
    path=tmp_path/'profile.json'
    path.write_text(json.dumps(p))
    state=poll._default_spool(p)
    state[field]=value
    original=json.dumps(state).encode()
    spool=poll.spool_path(path)
    spool.write_bytes(original)
    with pytest.raises(ClientError,match='scope|binding|channel|inbox'):
        poll.change_spool(path,p,lambda s:s.update(cursor=900))
    assert spool.read_bytes() == original
    assert json.loads(path.read_text())['ack_cursor'] == 0


def test_profile_migration_preserves_ack_and_backs_up_legacy_scope(tmp_path, monkeypatch):
    from argparse import Namespace

    from src import agentbus_client as client
    from src import agentbus_poll_client as poll
    from src.agentbus_channel_migration_client import migrate_profile
    from src.agentbus_scope_client import scope
    values = {'AGENTBUS_CONSUMER_STATE_DIR': str(tmp_path), 'AGENTBUS_URL': 'http://127.0.0.1:8766'}
    p = profile()
    p['ack_cursor'] = 7
    path = client.identity_path(values, p['identity'])
    path.write_text(json.dumps(p))
    monkeypatch.setattr(client, 'checked_profile', lambda *_args: (p, {}))
    original = json.dumps({'cursor': 19, 'events': [{'kind': 'MESSAGE', 'id': '19'}]}).encode()
    spool = poll.spool_path(path)
    spool.write_bytes(original)
    assert migrate_profile(Namespace(identity=p['identity']), values) == 0
    rebuilt = json.loads(spool.read_text())
    assert rebuilt['cursor'] == 7
    assert all(rebuilt[key] == value for key,value in scope(p).items())
    assert json.loads(path.read_text())['ack_cursor'] == 7
    assert list(tmp_path.glob('*.legacy-scope-*'))[0].read_bytes() == original
    assert migrate_profile(Namespace(identity=p['identity']), values) == 0


def test_profile_migration_rejects_foreign_projection_before_any_rewrite(tmp_path, monkeypatch):
    from argparse import Namespace

    from src import agentbus_client as client
    from src import agentbus_poll_client as poll
    from src import agentbus_wake_inbox_client as wake
    from src.agentbus_channel_migration_client import migrate_profile
    values = {'AGENTBUS_CONSUMER_STATE_DIR':str(tmp_path), 'AGENTBUS_URL':'http://127.0.0.1:8766'}
    p = profile()
    path = client.identity_path(values,p['identity'])
    path.write_text(json.dumps(p))
    monkeypatch.setattr(client,'checked_profile',lambda *_args:(p,{}))
    spool = poll.spool_path(path)
    spool.write_text(json.dumps({'cursor':1,'events':[]}))
    scan = wake.state_path(values,p['chat_id'])
    scan.parent.mkdir()
    scan.write_text(json.dumps({'channel':'C222','binding_generation':1}))
    before = (spool.read_bytes(),scan.read_bytes())
    with pytest.raises(ClientError,match='Foreign projection'):
        migrate_profile(Namespace(identity=p['identity']),values)
    assert before == (spool.read_bytes(),scan.read_bytes())
    assert not list(tmp_path.rglob('*.legacy-scope-*'))
