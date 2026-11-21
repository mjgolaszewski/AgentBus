"""Resume is a scope-bound observation, never acknowledgement or host restart."""
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src import agentbus_client as client
from src import agentbus_poll_client as poll
from src import agentbus_recovery_client as recovery
from src.agentbus_transport_client import ClientError


@pytest.fixture
def resume(tmp_path, monkeypatch):
    profile = {"identity": "alpha:agentbus:writer", "service_url": "http://127.0.0.1:8766", "channel": "C111",
               "chat_id": str(uuid4()), "inbox_id": str(uuid4()), "ack_cursor": 7,
               "participation": {"session_id": str(uuid4())}}
    presence = {"state": "active_compliant", "work_paused": False}
    controls = {"controls": []}
    values = {"AGENTBUS_URL": profile["service_url"], "AGENTBUS_CONSUMER_STATE_DIR": str(tmp_path)}
    calls = []
    monkeypatch.setattr(client, "resolve_identity", lambda value: value)
    monkeypatch.setattr(client, "control_profile", lambda *_: (profile, presence))
    monkeypatch.setattr(poll, "foreground_check_in", lambda *_: calls.append("check-in") or controls)
    monkeypatch.setattr(recovery, "scoped_api", lambda *args: calls.append(args[1]) or
                        {"messages": [], "next_cursor": 12, "has_more": False})
    yield profile, presence, controls, values, calls


def test_resume_preserves_ack_and_discloses_unknown_gaps(resume, tmp_path, capsys):
    profile, _, _, values, calls = resume
    before = copy.deepcopy(profile)
    ns = SimpleNamespace(identity=profile["identity"])
    assert recovery.cli_resume(ns, tmp_path, values) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["unmonitored_gap"]["from"] is None
    assert first["semantic_ack_cursor"] == 7 and first["read_is_not_acknowledgement"] is True
    assert profile == before and "after=7" in calls[-1]
    assert recovery.cli_resume(ns, tmp_path, values) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["unmonitored_gap"]["from"] == first["unmonitored_gap"]["to"]
    assert not second["unmonitored_gap"]["continuous_monitoring"]
    assert not any("ack" in path or "wake" in path for path in calls)
    assert "session_secret" not in next(tmp_path.glob(".resume-*.json")).read_text()


@pytest.mark.parametrize("hold", ["stopped", "pause_work", "stop_end_turn", "policy_ack"])
def test_resume_safety_controls_precede_mailbox(resume, tmp_path, capsys, hold):
    profile, presence, controls, values, calls = resume
    if hold == "stopped":
        presence["state"] = "stopped"
    elif hold == "policy_ack":
        controls.update({"ack_required": True})
    else:
        controls["controls"] = [{"kind": hold, "control_id": str(uuid4())}]
    assert recovery.cli_resume(SimpleNamespace(identity=profile["identity"]), tmp_path, values) == 0
    assert json.loads(capsys.readouterr().out)["mailbox"] is None
    assert calls == ([] if hold == "stopped" else ["check-in"])


def test_resume_rejects_foreign_checkpoint_before_controls_or_inbox(resume, tmp_path, capsys):
    profile, _, _, values, calls = resume
    ns = SimpleNamespace(identity=profile["identity"])
    recovery.cli_resume(ns, tmp_path, values)
    capsys.readouterr()
    checkpoint = next(tmp_path.glob(".resume-*.json"))
    altered = json.loads(checkpoint.read_text())
    altered["channel"] = "C222"
    checkpoint.write_text(json.dumps(altered))
    calls.clear()
    with pytest.raises(ClientError, match="scope differs"):
        recovery.cli_resume(ns, tmp_path, values)
    assert calls == [] and json.loads(checkpoint.read_text())["channel"] == "C222"


def test_profile_inventory_ignores_projections_and_preserves_legacy_profiles(resume, tmp_path):
    from src.agentbus_profile_client import profile_inventory
    profile, _, _, values, _ = resume
    for name, data in {
        profile['identity'] + '.json': profile,
        '.resume-scope.json': {key: profile[key] for key in ('service_url', 'inbox_id', 'channel', 'chat_id')},
        'alpha:agentbus:null.json': {key: profile[key] for key in ('service_url', 'inbox_id', 'channel', 'chat_id')},
        'alpha:agentbus:redirect.json': {**profile, 'identity': profile['identity']},
        'other:agentbus:foreign.json': {**profile, 'identity': 'other:agentbus:foreign', 'channel': 'C222'},
        'agentbus:legacy.json': {**profile, 'identity': 'agentbus:legacy', 'chat_id': str(uuid4())},
    }.items():
        (tmp_path / name).write_text(json.dumps(data))
    before = {path.name: path.read_bytes() for path in tmp_path.glob('*.json')}
    rows = profile_inventory(values, profile)
    assert {row['identity'] for row in rows} == {profile['identity'], 'agentbus:legacy'}
    assert all('participation' not in row and 'session_secret' not in row for row in rows)
    assert {path.name: path.read_bytes() for path in tmp_path.glob('*.json')} == before


def test_profile_inventory_skips_symlinks_and_non_regular_files(resume, tmp_path):
    import os

    from src.agentbus_profile_client import profile_inventory
    profile, _, _, values, _ = resume
    target = tmp_path / 'retained-evidence.json'
    target.write_text(json.dumps(profile))
    (tmp_path / (profile['identity'] + '.json')).symlink_to(target)
    pipe = tmp_path / 'alpha:agentbus:pipe.json'
    os.mkfifo(pipe)
    descriptor = os.open(pipe, os.O_RDWR | os.O_NONBLOCK)
    try:
        assert profile_inventory(values, profile) == []
    finally:
        os.close(descriptor)
    assert target.exists() and (tmp_path / (profile['identity'] + '.json')).is_symlink()


def context_value(profile, **changes):
    value = {'version': 1, 'scope': recovery.scope(profile), 'reported_at': 100.0,
             'source': {'commit': 'a'*40, 'tree': 'b'*40, 'branch': 'work/topic', 'dirty_paths': ['src/current.py']},
             'next_update_due_at': 200.0,
             'held_owners': [{'owner': 'alpha:operations:steward', 'receipt_cursor': 9, 'request_id': None}],
             'receipts': [{'kind': 'message', 'cursor': 8, 'request_id': str(uuid4())}]}
    value.update(changes)
    return value


def test_resume_context_binds_exact_scope_and_never_restores_authority(resume, tmp_path, capsys):
    import hashlib
    profile, _, _, values, calls = resume
    before = copy.deepcopy(profile)
    source = tmp_path/'context.json'
    source.write_text(json.dumps(context_value(profile)))
    raw = source.read_bytes()
    ns = SimpleNamespace(identity=profile['identity'], context_file=str(source))
    recovery.cli_resume(ns, tmp_path, values)
    result = json.loads(capsys.readouterr().out)
    context = result['recovery_context']
    assert context['input_sha256'] == hashlib.sha256(raw).hexdigest()
    assert context['execution_authority'] is False and context['automatic_update_or_retry'] is False
    assert result['semantic_ack_cursor'] == 7 and profile == before and source.read_bytes() == raw
    assert not any('wake' in path or '/ack' in path for path in calls)
    recovery.cli_resume(SimpleNamespace(identity=profile['identity']), tmp_path, values)
    assert json.loads(capsys.readouterr().out)['recovery_context']['input_sha256'] == context['input_sha256']


@pytest.mark.parametrize('case', ['foreign', 'credential_field', 'known_secret', 'oversize', 'symlink', 'invalid_version'])
def test_bad_context_preserves_checkpoint_before_checkin_or_mailbox(resume, tmp_path, capsys, case):
    profile, _, _, values, calls = resume
    recovery.cli_resume(SimpleNamespace(identity=profile['identity']), tmp_path, values)
    capsys.readouterr()
    checkpoint = next(tmp_path.glob('.resume-*.json'))
    before = checkpoint.read_bytes()
    value = context_value(profile)
    if case == 'foreign':
        value['scope']['channel'] = 'C222'
    elif case == 'credential_field':
        value['session_secret'] = 'not-a-context-field'
    elif case == 'known_secret':
        profile['participation']['session_secret'] = 's'*40
        value['source']['branch'] = 's'*40
    elif case == 'invalid_version':
        value['version'] = True
    source = tmp_path/'provided.json'
    source.write_text('x'*16385 if case == 'oversize' else json.dumps(value))
    if case == 'symlink':
        link = tmp_path/'context-link.json'
        link.symlink_to(source)
        source = link
    calls.clear()
    with pytest.raises(ClientError):
        recovery.cli_resume(SimpleNamespace(identity=profile['identity'], context_file=str(source)), tmp_path, values)
    assert calls == [] and checkpoint.read_bytes() == before and source.exists()


def test_context_cannot_bypass_stop_or_create_a_new_request(resume, tmp_path, capsys):
    profile, presence, _, values, calls = resume
    presence['state'] = 'stopped'
    source = tmp_path/'context.json'
    source.write_text(json.dumps(context_value(profile, next_update_due_at=1)))
    recovery.cli_resume(SimpleNamespace(identity=profile['identity'], context_file=str(source)), tmp_path, values)
    result = json.loads(capsys.readouterr().out)
    assert result['mailbox'] is None and result['recovery_context']['deadline_state'] == 'past_due'
    assert calls == [] and not list(tmp_path.glob('send-requests/*'))


def test_resume_preserves_checkpoint_on_clock_regression(resume, tmp_path, capsys, monkeypatch):
    profile, _, _, values, calls = resume
    ns = SimpleNamespace(identity=profile['identity'])
    recovery.cli_resume(ns, tmp_path, values)
    capsys.readouterr()
    checkpoint = next(tmp_path.glob('.resume-*.json'))
    before = checkpoint.read_bytes()
    calls.clear()
    monkeypatch.setattr(recovery.time, 'time', lambda: 0.0)
    with pytest.raises(ClientError, match='clock precedes'):
        recovery.cli_resume(ns, tmp_path, values)
    assert calls == [] and checkpoint.read_bytes() == before


def test_resume_context_does_not_overwrite_a_concurrent_checkpoint(resume, tmp_path, capsys, monkeypatch):
    profile, _, _, values, _ = resume
    ns = SimpleNamespace(identity=profile['identity'])
    recovery.cli_resume(ns, tmp_path, values)
    capsys.readouterr()
    checkpoint = next(tmp_path.glob('.resume-*.json'))
    newer = json.loads(checkpoint.read_text())
    newer['observed_at'] += 1
    def page(*args):
        checkpoint.write_text(json.dumps(newer))
        return {'messages': [], 'next_cursor': 12, 'has_more': False}
    monkeypatch.setattr(recovery, 'scoped_api', page)
    with pytest.raises(ClientError, match='changed concurrently'):
        recovery.cli_resume(ns, tmp_path, values)
    assert json.loads(checkpoint.read_text()) == newer


@pytest.mark.parametrize('corrupt', ['fifo', 'deep_json'])
def test_resume_rejects_nonregular_or_deep_checkpoint_without_hanging(resume, tmp_path, capsys, corrupt):
    import hashlib
    import os
    profile, _, _, values, calls = resume
    checkpoint = tmp_path/('.resume-'+hashlib.sha256(profile['chat_id'].encode()).hexdigest()+'.json')
    descriptor = None
    if corrupt == 'fifo':
        os.mkfifo(checkpoint, 0o600)
        descriptor = os.open(checkpoint, os.O_RDWR | os.O_NONBLOCK)
    else:
        checkpoint.write_text('['*1100+']'*1100)
        checkpoint.chmod(0o600)
    try:
        with pytest.raises(ClientError, match='checkpoint is invalid'):
            recovery.cli_resume(SimpleNamespace(identity=profile['identity']), tmp_path, values)
        assert calls == [] and checkpoint.exists()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def test_operator_contact_hint_is_scoped_expiring_and_never_resource_admission(tmp_path):
    import json
    from dataclasses import replace

    from src.agentbus_recovery_contact_service import contact_snapshot, decode_contacts
    from src.agentbus_service import MessageStore, SendMessage
    from src.agentbus_settings_service import Settings
    store=MessageStore(tmp_path/'contact.sqlite3')
    store.channels.configure(('C111','C222'))
    store.participation.initialize_default_policy()
    args=dict(channel='C111',repo='demo',route='local:demo:keeper',actor='operator')
    grant=store.channels.issue_grant(**args)
    store.channels.enroll(grant,repo='demo',route=args['route'],display_name='Keeper',session_secret='s'*40)
    store.append('C111','100.1',SendMessage(sender='human',text='Private accepted context'))
    row={'channel':'C111','route':args['route'],'observed_at':100,'expires_at':200,
         'receipt_position':1,'service_epoch':None,'host_epoch':None}
    contacts=decode_contacts(json.dumps({'version':1,'contacts':[row]}),('C111','C222'))
    settings=Settings(api_token='test',slack_bot_token='test',slack_app_token='test',slack_channel='',
                      slack_channels=('C111','C222'),db_path=tmp_path/'contact.sqlite3',recovery_contacts=contacts)
    before=store._db.total_changes
    try:
        snapshot=contact_snapshot(settings,store,'C111',instant=150)
        assert snapshot['state']=='unexpired_report' and snapshot['resource_admission'] is False
        assert snapshot['authorship']=='operator_configuration' and snapshot['epoch_validation']=='unverified_or_unknown'
        assert snapshot['receipt']=={'position':1,'state':'retained_reference'}
        assert 'Private accepted context' not in str(snapshot)
        assert contact_snapshot(settings,store,'C222',instant=150)=={'configured':False,'state':'unknown','resource_admission':False}
        assert contact_snapshot(settings,store,'C111',instant=200)['state']=='expired_report'
        assert contact_snapshot(settings,store,'C111',instant=50)['state']=='future_report'
        unknown=replace(settings,recovery_contacts=decode_contacts(json.dumps({'version':1,'contacts':[
            {**row,'observed_at':None,'expires_at':None}]}),settings.channels))
        assert contact_snapshot(unknown,store,'C111',instant=150)['state']=='unknown_freshness'
        store.channels.enabled=frozenset({'C222'})
        assert contact_snapshot(settings,store,'C111',instant=150)['contact'] is None
        assert store._db.total_changes==before
    finally:
        store.close()


@pytest.mark.parametrize('change', [
    {'channel':'CFOREIGN'}, {'route':'slack:user'}, {'observed_at':True}, {'expires_at':100},
    {'expires_at':100+8*86400}, {'receipt_position':True}, {'receipt_position':0},
    {'service_epoch':'invented'}, {'host_epoch':'not-a-uuid'}, {'token':'forbidden'},
])
def test_recovery_contact_configuration_rejects_foreign_unbounded_or_credential_fields(change):
    import json

    from src.agentbus_recovery_contact_service import decode_contacts
    row={'channel':'C111','route':'local:demo:keeper','observed_at':100,'expires_at':200,
         'receipt_position':None,'service_epoch':None,'host_epoch':None}
    with pytest.raises(ValueError,match='Invalid bounded'):
        decode_contacts(json.dumps({'version':1,'contacts':[{**row,**change}]}),('C111',))



def test_compact_resume_skips_terminal_history_but_preserves_unknown_recovery_and_ack(resume,tmp_path,monkeypatch,capsys):
    profile,_,_,values,_=resume
    profile['message_lifecycle_version']=1
    calls=[]
    pages=[{'messages':[{'cursor':8+i,'lifecycle':{'state':'handled'}} for i in range(20)],'next_cursor':27,'has_more':True},
           {'messages':[{'cursor':28,'lifecycle':{'state':'recovery_required'}},{'cursor':29,'lifecycle':None},
                        {'cursor':30,'lifecycle':{'state':'unclaimed'}}],'next_cursor':30,'has_more':False}]
    def get(_values,path,_profile):
        calls.append(path)
        return pages.pop(0)
    monkeypatch.setattr(recovery,'scoped_api',get)
    before=copy.deepcopy(profile)
    recovery.cli_resume(SimpleNamespace(identity=profile['identity'],unhandled=True),tmp_path,values)
    result=json.loads(capsys.readouterr().out)
    assert [m['cursor'] for m in result['mailbox']['messages']]==[28,29,30]
    assert result['mailbox']['resume_projection']['terminal_omitted']==20
    assert result['mailbox']['resume_projection']['scan_limit_reached'] is False
    assert len(calls)==2 and 'after=27' in calls[1] and profile==before


def test_compact_resume_scan_is_finite_and_never_claims_no_more_work(resume,tmp_path,monkeypatch,capsys):
    profile,_,_,values,_=resume
    profile['message_lifecycle_version']=1
    calls=[]
    def get(_values,path,_profile):
        calls.append(path)
        first=8+(len(calls)-1)*20
        return {'messages':[{'cursor':i,'lifecycle':{'state':'handled'}} for i in range(first,first+20)],
                'next_cursor':first+19,'has_more':True}
    monkeypatch.setattr(recovery,'scoped_api',get)
    recovery.cli_resume(SimpleNamespace(identity=profile['identity'],unhandled=True),tmp_path,values)
    result=json.loads(capsys.readouterr().out)
    assert len(calls)==5 and result['mailbox']['messages']==[]
    assert result['mailbox']['has_more'] is True
    assert result['mailbox']['resume_projection']['scan_limit_reached'] is True
    assert profile['ack_cursor']==7


def test_compact_resume_requires_lifecycle_without_inferring_terminal_history(resume,tmp_path,monkeypatch):
    profile,_,_,values,calls=resume
    with pytest.raises(ClientError,match='lifecycle'):
        recovery.cli_resume(SimpleNamespace(identity=profile['identity'],unhandled=True),tmp_path,values)
    assert calls==['check-in']


@pytest.mark.parametrize("hold",["stopped","pause_work","stop_end_turn","policy_ack"])
def test_compact_resume_checks_controls_even_without_lifecycle_support(resume,tmp_path,capsys,hold):
    profile,presence,controls,values,calls=resume
    if hold=="stopped":
        presence["state"]="stopped"
    elif hold=="policy_ack":
        controls["ack_required"]=True
    else:
        controls["controls"]=[{"kind":hold,"control_id":str(uuid4())}]
    recovery.cli_resume(SimpleNamespace(identity=profile["identity"],unhandled=True),tmp_path,values)
    assert json.loads(capsys.readouterr().out)["mailbox"] is None
    assert calls==([] if hold=="stopped" else ["check-in"])


def test_compact_resume_preserves_retired_gap_from_earlier_scan_page(resume,tmp_path,monkeypatch,capsys):
    profile,_,_,values,_=resume
    profile["message_lifecycle_version"]=1
    pages=[{"messages":[{"cursor":8,"lifecycle":{"state":"handled"}}],"next_cursor":8,"has_more":True,"retired_history":True},
           {"messages":[{"cursor":9,"lifecycle":{"state":"unclaimed"}}],"next_cursor":9,"has_more":False,"retired_history":False}]
    monkeypatch.setattr(recovery,"scoped_api",lambda *_:pages.pop(0))
    recovery.cli_resume(SimpleNamespace(identity=profile["identity"],unhandled=True),tmp_path,values)
    result=json.loads(capsys.readouterr().out)
    assert result["mailbox"]["retired_history"] is True
    assert profile["ack_cursor"]==7
