"""Identity previews bind verified context; decoding is never execution authority."""
import copy
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from src.agentbus_identity_intent_service import (
    decode_identity_intent,
    encode_identity_intent,
    intent_digest,
    intent_mapping,
    require_fresh_intent,
)


def preview(**changes):
    value = {'version': 1, 'request_id': str(uuid4()), 'actor': 'slack:UOP', 'channel': 'C111',
             'inbox_id': 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'chat_id': str(uuid4()), 'revision': 7, 'action': 'retire', 'created_at': 100.0,
             'expires_at': 500.0, 'name': None, 'new_route': None, 'session_ids': [str(uuid4())], 'positions': []}
    value.update(changes)
    if value['action']=='rename' and 'new_route' not in changes and isinstance(value['name'],str):
        value['new_route']='local:demo:'+value['name']
    return value


def decode(value):
    return decode_identity_intent(value, actor='slack:UOP', channel='C111', inbox_id='cccccccc-cccc-4ccc-8ccc-cccccccccccc')


def test_identity_intent_is_immutable_and_round_trips_without_side_effects():
    value = preview(action='retire_with_history', positions=[8, 2], session_ids=[str(uuid4()), str(uuid4())])
    before = copy.deepcopy(value)
    intent = decode(value)
    assert value == before and intent.positions == (2, 8)
    import json
    assert decode(json.loads(encode_identity_intent(intent))) == intent
    assert intent_digest(decode({**value, 'positions': [2, 8]})) == intent_digest(intent)
    with pytest.raises(FrozenInstanceError):
        intent.actor = 'slack:OTHER'


@pytest.mark.parametrize('change', [
    {'version': True}, {'version': 2}, {'actor': 'slack:OTHER'}, {'channel': 'C222'},
    {'inbox_id': str(uuid4())}, {'revision': True}, {'revision': -1}, {'revision': 2**63}, {'action': 'delete_anything'},
    {'created_at': float('nan')}, {'expires_at': float('inf')}, {'created_at': True},
    {'expires_at': 100.0}, {'expires_at': 700.01}, {'name': 'unexpected'},
    {'request_id': 'not-a-uuid'}, {'chat_id': 'foreign'}, {'positions': [1]},
    {'session_ids': [str(uuid4())]*257}, {'positions': list(range(1, 102)), 'action': 'retire_with_history'},
    {'positions': [True], 'action': 'retire_with_history'},
    {'positions': [2, 2], 'action': 'retire_with_history'},
    {'session_ids': ['aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa']*2},
    {'name': 'Upper Case', 'action': 'rename'}, {'name': None, 'action': 'rename'},
    {'grant': 'not-authority'},
])
def test_identity_intent_rejects_substitution_and_unbounded_shapes(change):
    with pytest.raises(ValueError, match='Identity operation unavailable'):
        decode(preview(**change))


@pytest.mark.parametrize('instant', [99.0, 500.0, True, float('nan'), float('inf')])
def test_expired_or_unknown_preview_never_becomes_fresh_admission(instant):
    value = preview()
    intent = decode(value)  # Historical decoding remains available to receipt readers.
    with pytest.raises(ValueError):
        require_fresh_intent(intent, instant)
    assert intent_mapping(intent)['request_id'] == value['request_id']


def test_identity_rename_shape_and_freshness_do_not_select_another_scope():
    intent = decode(preview(action='rename', name='new-name'))
    require_fresh_intent(intent, 100.0)
    assert intent.name == 'new-name' and intent.channel == 'C111'
    with pytest.raises(ValueError):
        decode_identity_intent(intent_mapping(intent), actor='slack:UOP', channel='C222', inbox_id='cccccccc-cccc-4ccc-8ccc-cccccccccccc')


@pytest.fixture
def managed(tmp_path):
    from src.agentbus_identity_management_service import IdentityManagement
    from src.agentbus_service import MessageStore
    from src.agentbus_settings_service import Settings
    store = MessageStore(tmp_path/'identities.sqlite3')
    store.channels.configure(('C111','C222'))
    store.participation.initialize_default_policy()
    settings = Settings(api_token='test',slack_bot_token='test',slack_app_token='test',slack_channel='',
                        slack_channels=('C111','C222'),slack_operator_user_ids=frozenset({'UOP'}),
                        local_prefix='local',db_path=tmp_path/'identities.sqlite3')
    identities = {}
    for name,channel in [('first','C111'),('second','C111'),('foreign','C222')]:
        route = 'local:demo:'+name
        secret = (name+'-private-').ljust(40,'x')
        grant = store.channels.issue_grant(channel=channel,repo='demo',route=route,actor='operator')
        chat,session,revision = store.channels.enroll(grant,repo='demo',route=route,display_name=name,session_secret=secret)
        store.participation.acknowledge_policy(session,secret,revision)
        identities[name]=(chat,session,secret)
    owner = IdentityManagement(store,settings,clock=lambda:100.0)
    try:
        yield store,settings,owner,identities
    finally:
        store.close()


def test_operator_retirement_revokes_access_without_stop_ack_or_host_action(managed):
    store,_,owner,ids = managed
    chat,session,secret = ids['first']
    before = store._db.execute('SELECT count(*) FROM control_targets').fetchone()[0]
    preview = owner.preview('UOP','C111',chat,'retire')
    result = owner.perform('UOP','C111',preview['intent']['request_id'],preview['digest'])
    assert result['sessions_revoked']==1 and result['semantic_acknowledgement']=='not_inferred'
    assert store.participation.session_state(session)['state']=='stopped'
    assert store._db.execute('SELECT count(*) FROM control_targets').fetchone()[0]==before
    with pytest.raises(PermissionError):
        store.channels.principal(secret)
    assert store.channels.principal(secret,receipt_recovery=True).chat_id==chat
    assert not store.channels.local_target('C111','local:demo:first')
    assert owner.perform('UOP','C111',preview['intent']['request_id'],preview['digest'])==result
    assert owner.receipt('UOP','C111',result['request_id'])==result
    assert owner.identities('UOP','C111')['identities'][0]['chat_id']==ids['second'][0]
    assert len(owner.identities('UOP','C111',include_retired=True)['identities'])==2


@pytest.mark.parametrize('actor,channel', [('UOTHER','C111'),('UOP','CFOREIGN'),('UOP','C222')])
def test_identity_operator_authority_and_channel_precede_target_mutation(managed,actor,channel):
    store,_,owner,ids=managed
    before=store._db.total_changes
    with pytest.raises((ValueError,PermissionError)):
        owner.preview(actor,channel,ids['first'][0],'retire')
    assert store._db.total_changes==before


def test_identity_preview_expires_and_cannot_be_substituted_or_reused_after_revision(managed):
    store,settings,owner,ids=managed
    from src.agentbus_identity_management_service import IdentityManagement
    chat,session,secret=ids['first']
    preview=owner.preview('UOP','C111',chat,'rename',name='renamed')
    key=preview['intent']['request_id']
    with pytest.raises(ValueError):
        owner.perform('UOP','C111',key,'0'*64)
    with pytest.raises(ValueError):
        IdentityManagement(store,settings,clock=lambda:700).perform('UOP','C111',key,preview['digest'])
    assert owner.receipt('UOP','C111',key)['state']=='preview'
    store.participation.rename(session,secret,'local:demo:intervening')
    with pytest.raises(ValueError,match='changed after preview'):
        owner.perform('UOP','C111',key,preview['digest'])
    assert store.channels.principal(secret).route=='local:demo:intervening'


def test_operator_rename_preserves_membership_aliases_and_exact_session(managed):
    store,_,owner,ids=managed
    chat,session,secret=ids['first']
    old=store.channels.principal(secret)
    preview=owner.preview('UOP','C111',chat,'rename',name='renamed')
    result=owner.perform('UOP','C111',preview['intent']['request_id'],preview['digest'])
    new=store.channels.principal(secret)
    assert (new.chat_id,new.session_id,new.channel,new.inbox_id)==(old.chat_id,old.session_id,old.channel,old.inbox_id)
    assert new.route=='local:demo:renamed' and result['retired'] is False
    assert store.channels.local_target('C111',old.route)
    assert store.participation.session_state(session)['state']=='active'
    assert owner.resolve('UOP','C111','renamed')==chat


def test_identity_history_unavailable_never_becomes_unconditional_delete(managed):
    store,_,owner,ids=managed
    chat=ids['first'][0]
    with pytest.raises(ValueError,match='eligibility is unavailable'):
        owner.preview('UOP','C111',chat,'retire_with_history')
    assert store.channels.principal(ids['first'][2]).chat_id==chat


def test_private_identity_buttons_bind_actor_channel_and_exact_confirmation(managed):
    import json

    from src.agentbus_slack_command_service import execute_command
    from src.agentbus_slack_ui_commands_service import execute_interaction
    store,settings,_,ids=managed
    reply=execute_command({'command':'/agentbus','channel_id':'C111','user_id':'UOP','text':'identities'},settings,store,None)
    button=next(b['elements'][0] for b in reply.blocks if b['type']=='actions')
    assert button['action_id'].startswith('agentbus.operator.identity_detail:')
    def payload(choice,user='UOP',channel='C111'):
        return {'type':'block_actions','user':{'id':user},'channel':{'id':channel},
                'container':{'type':'message','channel_id':channel},'actions':[choice]}
    detail=execute_interaction(payload(button),settings,store,None)
    preview_button=next(b['elements'][0] for b in detail.blocks if b['type']=='actions')
    preview=execute_interaction(payload(preview_button),settings,store,None)
    confirm=next(b['elements'][0] for b in preview.blocks if b['type']=='actions')
    raw=json.loads(confirm['value'])
    assert 'digest' in raw
    denied=execute_interaction(payload(confirm,user='UOTHER'),settings,store,None)
    assert 'not enabled' in denied
    assert 'another channel' in execute_interaction(payload(confirm,channel='C222'),settings,store,None)
    assert store.participation.session_state(ids['first'][1])['state']=='active'
    result=execute_interaction(payload(confirm),settings,store,None)
    assert result.outcome=='committed' and result.receipt==raw['target']
    assert execute_interaction(payload(confirm),settings,store,None)==result


def test_operator_rename_confirmation_binds_full_route_across_configuration_restart(managed):
    from dataclasses import replace

    from src.agentbus_identity_management_service import IdentityManagement
    store,settings,owner,ids=managed
    preview=owner.preview('UOP','C111',ids['first'][0],'rename',name='exact-name')
    assert preview['intent']['new_route']=='local:demo:exact-name'
    changed=IdentityManagement(store,replace(settings,local_prefix='different'),clock=lambda:101)
    with pytest.raises(ValueError,match='configuration changed'):
        changed.perform('UOP','C111',preview['intent']['request_id'],preview['digest'])
    assert store.channels.principal(ids['first'][2]).route=='local:demo:first'
    assert owner.receipt('UOP','C111',preview['intent']['request_id'])['state']=='preview'


def test_recipient_retirement_preserves_exact_accepted_replay_but_blocks_new_admission(managed):
    from fastapi import HTTPException

    from src.agentbus_channel_api_service import require_target
    from src.agentbus_local_delivery_service import fingerprint
    from src.agentbus_service import SendMessage
    store,_,owner,ids=managed
    sender=store.channels.principal(ids['first'][2])
    body=SendMessage(sender=sender.route,recipient='local:demo:second',text='Original accepted work',audience='direct')
    body._sender_assurance='session'
    key=str(uuid4())
    def check():
        return require_target(store,'C111',body)
    accepted=store.local_delivery.accept('C111',key,fingerprint(body.model_dump()),body,'session:'+sender.session_id,
                                         session_token=ids['first'][2],target_check=check)
    preview=owner.preview('UOP','C111',ids['second'][0],'retire')
    owner.perform('UOP','C111',preview['intent']['request_id'],preview['digest'])
    replay=store.local_delivery.accept('C111',key,fingerprint(body.model_dump()),body,'session:'+sender.session_id,
                                      session_token=ids['first'][2],target_check=check)
    assert replay.cursor==accepted.cursor
    before=store._db.execute('SELECT count(*) FROM local_delivery_obligations').fetchone()[0]
    with pytest.raises(HTTPException) as denied:
        store.local_delivery.accept('C111',str(uuid4()),fingerprint(body.model_dump()),body,'session:'+sender.session_id,
                                     session_token=ids['first'][2],target_check=check)
    assert denied.value.status_code==403
    assert store._db.execute('SELECT count(*) FROM local_delivery_obligations').fetchone()[0]==before


def test_private_identity_capacity_observes_preview_and_commit_without_expiring_receipts(managed):
    from src.agentbus_identity_management_service import identity_capacity
    store,_,owner,ids=managed
    preview=owner.preview('UOP','C111',ids['first'][0],'retire')
    before=store._db.total_changes
    assert identity_capacity(store)['states']=={'preview':1}
    assert store._db.total_changes==before
    owner.perform('UOP','C111',preview['intent']['request_id'],preview['digest'])
    assert identity_capacity(store)['states']=={'committed':1}
    assert identity_capacity(store)['retained']==1 and identity_capacity(store)['limit']==4096


def test_identity_preview_and_committed_receipt_survive_service_restart(managed):
    from src.agentbus_identity_management_service import IdentityManagement
    from src.agentbus_service import MessageStore
    store,settings,owner,ids=managed
    preview=owner.preview('UOP','C111',ids['first'][0],'rename',name='after-restart')
    key=preview['intent']['request_id']
    store.close()
    reopened=MessageStore(settings.db_path)
    reopened.channels.configure(settings.channels)
    try:
        recovered=IdentityManagement(reopened,settings,clock=lambda:101)
        assert recovered.receipt('UOP','C111',key)['intent']==preview['intent']
        result=recovered.perform('UOP','C111',key,preview['digest'])
        assert result['route']=='local:demo:after-restart'
    finally:
        reopened.close()
    final=MessageStore(settings.db_path)
    final.channels.configure(settings.channels)
    try:
        recovered=IdentityManagement(final,settings,clock=lambda:1000)
        assert recovered.receipt('UOP','C111',key)==result
        assert recovered.perform('UOP','C111',key,preview['digest'])==result
        assert final.channels.principal(ids['first'][2]).chat_id==ids['first'][0]
    finally:
        final.close()


def test_concurrent_identity_confirmations_share_one_revocation_receipt(managed):
    from concurrent.futures import ThreadPoolExecutor
    store,_,owner,ids=managed
    preview=owner.preview('UOP','C111',ids['first'][0],'retire')
    def confirm(_):
        return owner.perform('UOP','C111',preview['intent']['request_id'],preview['digest'])
    with ThreadPoolExecutor(max_workers=2) as executor:
        first,second=executor.map(confirm,range(2))
    assert first==second and first['sessions_revoked']==1
    assert store._db.execute("SELECT count(*) FROM participation_audit WHERE action='operator_identity_revocation'").fetchone()[0]==1


def test_identity_confirmation_rechecks_revoked_operator_and_retains_preview(managed):
    from dataclasses import replace

    from src.agentbus_identity_management_service import IdentityManagement
    store,settings,owner,ids=managed
    preview=owner.preview('UOP','C111',ids['first'][0],'retire')
    revoked=IdentityManagement(store,replace(settings,slack_operator_user_ids=frozenset()),clock=lambda:101)
    with pytest.raises(PermissionError):
        revoked.perform('UOP','C111',preview['intent']['request_id'],preview['digest'])
    assert owner.receipt('UOP','C111',preview['intent']['request_id'])['state']=='preview'
    assert store.participation.session_state(ids['first'][1])['state']=='active'


def test_identity_preview_capacity_never_discards_a_retained_operation(managed,monkeypatch):
    import src.agentbus_identity_management_service as domain
    _,_,owner,ids=managed
    monkeypatch.setattr(domain,'MAX_OPERATIONS',1)
    preview=owner.preview('UOP','C111',ids['first'][0],'retire')
    with pytest.raises(ValueError,match='capacity'):
        owner.preview('UOP','C111',ids['second'][0],'retire')
    assert owner.receipt('UOP','C111',preview['intent']['request_id'])['state']=='preview'


def test_identity_detail_and_nonhistory_previews_never_run_eligibility(managed, monkeypatch):
    store,_,owner,ids=managed
    store.lifecycle.activate_fresh(instant=100)
    def forbidden(*args,**kwargs):
        raise AssertionError('ordinary identity view scanned deletion eligibility')
    monkeypatch.setattr(store.retention,'_eligible',forbidden)
    chat=ids['first'][0]
    assert owner.detail('UOP','C111',chat)['history']['eligibility_checked'] is False
    for action,name in [('retire',None),('rename','renamed')]:
        preview=owner.preview('UOP','C111',chat,action,name=name)
        assert preview['history']['eligibility_checked'] is False
        assert preview['intent']['positions']==[]
    assert store.participation.session_state(ids['first'][1])['state']=='active'


def test_identity_history_query_limit_releases_lock_and_preserves_custody(managed,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from src.agentbus_message_lifecycle_service import LifecycleRejected
    import src.agentbus_retention_service as retention_module
    store,_,owner,ids=managed
    store.lifecycle.activate_fresh(instant=100)
    before=store._db.total_changes
    monkeypatch.setattr(retention_module,'QUERY_WORK_STEPS',1)
    with pytest.raises(LifecycleRejected,match='work limit'):
        owner.preview('UOP','C111',ids['first'][0],'retire_with_history')
    assert store._db.execute('SELECT count(*) FROM identity_management_operations').fetchone()[0]==0
    assert not store._db.in_transaction
    monkeypatch.setattr(retention_module,'QUERY_WORK_STEPS',5000000)
    with ThreadPoolExecutor(max_workers=1) as pool:
        result=pool.submit(owner.detail,'UOP','C111',ids['first'][0]).result(timeout=2)
    assert result['retired'] is False
    assert store._db.total_changes==before


def test_identity_view_labels_history_as_unassessed(managed):
    from src.agentbus_identity_ui_service import identity_detail
    store,_,owner,ids=managed
    reply=identity_detail(owner,'UOP','C111',ids['first'][0],'/custombus')
    assert 'not assessed' in reply.text
    assert 'eligible batch `0`' not in reply.text
