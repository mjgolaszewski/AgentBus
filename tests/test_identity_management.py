"""Identity previews bind verified context; decoding is never execution authority."""
import copy
from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from src.agentbus_identity_intent_service import (
    decode_identity_intent, encode_identity_intent, intent_digest, intent_mapping, require_fresh_intent,
)


def preview(**changes):
    value = {'version': 1, 'request_id': str(uuid4()), 'actor': 'slack:UOP', 'channel': 'C111',
             'inbox_id': 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', 'chat_id': str(uuid4()), 'revision': 7, 'action': 'retire', 'created_at': 100.0,
             'expires_at': 500.0, 'name': None, 'session_ids': [str(uuid4())], 'positions': []}
    value.update(changes)
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
