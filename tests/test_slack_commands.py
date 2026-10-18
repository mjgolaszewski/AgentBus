"""Operator Slack commands use verified users and immutable session authority."""

from __future__ import annotations

from dataclasses import replace

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

from src.agentbus_service import MessageStore, SlackReceiver
from src.agentbus_settings_service import Settings
from src.agentbus_slack_command_service import execute_command, resolve_session

POLICY = {
    "initial_interval_seconds": 60, "backoff_factor": 2,
    "max_interval_seconds": 1920, "control_check_max_seconds": 60,
    "jitter_fraction": 0, "overdue_grace_seconds": 120,
    "presentation_budget_bytes": None,
}
SECRET = "session-secret-with-at-least-32-characters"


class FakeWeb:
    def __init__(self):
        self.posts = []
        self.ephemeral = []

    def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "channel": kwargs["channel"], "ts": f"1700000000.{len(self.posts):06}"}

    def chat_postEphemeral(self, **kwargs):
        self.ephemeral.append(kwargs)
        return {"ok": True}


class FakeSocket:
    def __init__(self, web_client):
        self.web_client = web_client
        self.acks = []

    def send_socket_mode_response(self, response):
        self.acks.append(response)


@pytest.fixture
def enrolled(tmp_path):
    settings = Settings(api_token="x" * 32, slack_bot_token="xoxb-test",
                        slack_app_token="xapp-test", slack_channel="C123456",
                        db_path=tmp_path / "messages.sqlite3",
                        slack_operator_user_ids=frozenset({"U123"}))
    store = MessageStore(settings.db_path)
    store.channels.configure(settings.channels)
    store.participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    _, session_id, revision = store.participation.enroll(
        repo="agentbus", route="agentbus:flower", display_name="Flower", session_secret=SECRET)
    store.participation.acknowledge_policy(session_id, SECRET, revision)
    yield settings, store, session_id, FakeWeb()
    store.close()


def command(settings, store, web, text, *, user="U123", channel="C123456"):
    return execute_command({"command": "/agentbus", "channel_id": channel,
                            "user_id": user, "text": text}, settings, store, web)


def test_pause_resume_are_idempotent_and_preserve_participation(enrolled):
    settings, store, session_id, web = enrolled
    short = session_id[-6:]
    assert "not enabled" in command(settings, store, web, f"pause {short}", user="U999")
    assert store.participation.presence(session_id)["work_paused"] is False
    assert "Work paused" in command(settings, store, web, f"pause {short}")
    assert "already paused" in command(settings, store, web, f"pause {short}")
    assert store.participation.presence(session_id)["work_paused"] is True
    assert store.controls.deliver(session_id, SECRET)[0]["kind"] == "pause_work"
    assert "work: paused" in command(settings, store, web, f"status {short}")
    assert "Work resumed" in command(settings, store, web, f"resume {short}")
    assert {item["kind"] for item in store.controls.deliver(session_id, SECRET)} == {
        "pause_work", "resume_work"}
    assert store.participation.presence(session_id)["state"] == "control_pending"
    assert store.participation.presence(session_id)["work_paused"] is False


def test_retire_requires_confirmation_and_stopped_session_cannot_resume(enrolled):
    settings, store, session_id, web = enrolled
    short = session_id[-6:]
    assert "irreversible" in command(settings, store, web, f"retire {short}")
    assert store.participation.presence(session_id)["state"] == "active_compliant"
    assert "Durable stop issued" in command(settings, store, web, f"retire {short} CONFIRM")
    controls = store.controls.deliver(session_id, SECRET)
    assert len(controls) == 1
    store.controls.acknowledge_stop(controls[0]["control_id"], session_id, SECRET)
    assert store.participation.presence(session_id)["state"] == "stopped"
    assert "Retired sessions" in command(settings, store, web, f"resume {short}")


def test_roster_metrics_send_and_checkpoint_use_same_short_resolver(enrolled):
    settings, store, session_id, web = enrolled
    short = session_id[-6:]
    assert short in command(settings, store, web, "roster")
    assert "Acknowledged inbox cursor" in command(settings, store, web, f"metrics {short}")
    assert "Delivered" in command(settings, store, web, f"wake {short} Please report progress")
    assert "Delivered" in command(settings, store, web, f"send {short} Another request")
    assert "Please report progress" in str(web.posts[0]["blocks"])
    assert store.inbox(settings.slack_channel, "agentbus:flower", 0, 20, None, False).messages[0].sender_assurance == "slack-human"
    assert "Checkpoint requested" in command(settings, store, web, f"checkpoint {short}")
    assert "received: 2" in command(settings, store, web, f"metrics {short}")


def test_suffix_collision_and_wrong_channel_fail_closed(enrolled):
    settings, store, session_id, web = enrolled
    assert "configured channel" in command(settings, store, web, "roster", channel="COTHER")
    assert "at least six" in command(settings, store, web, "pause 123")
    _, second, revision = store.participation.enroll(
        repo="agentbus", route="agentbus:other", display_name="Other", session_secret=SECRET+"-other")
    store.participation.acknowledge_policy(second, SECRET+"-other", revision)
    collision = second[:-6] + session_id[-6:]
    store._db.execute("UPDATE participation_sessions SET session_id = ? WHERE session_id = ?", (collision, second))
    store._db.commit()
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_session(store, session_id[-6:])
    assert "ambiguous" in command(settings, store, web, f"pause {session_id[-6:]}")
    assert store.participation.presence(session_id)["work_paused"] is False


def test_empty_allowlist_disables_operator_commands(enrolled):
    settings, store, session_id, web = enrolled
    disabled = replace(settings, slack_operator_user_ids=frozenset())
    assert "not enabled" in command(disabled, store, web, f"pause {session_id[-6:]}")


def test_help_lists_every_command_and_is_private_to_authorized_users(enrolled):
    settings, store, _session_id, web = enrolled
    help_text = command(settings, store, web, "help")
    for name in ("roster", "status", "metrics", "send", "wake", "checkpoint", "pause", "resume", "retire"):
        assert name in help_text
    assert "CONFIRM" in help_text and "6–12" in help_text
    assert "not enabled" in command(settings, store, web, "help", user="U999")


def test_socket_ack_precedes_work_and_duplicate_invocation_is_not_reexecuted(enrolled):
    settings, store, session_id, web = enrolled
    receiver = SlackReceiver(store, settings.slack_channel, "B123", settings)
    socket = FakeSocket(web)
    request = SocketModeRequest(type="slash_commands", envelope_id="env-1", payload={
        "command": "/agentbus", "channel_id": settings.slack_channel,
        "user_id": "U123", "trigger_id": "same-invocation", "text": f"pause {session_id[-6:]}",
    })
    receiver(socket, request)
    receiver._commands.shutdown(wait=True)
    assert len(socket.acks) == 1 and "Checking" in socket.acks[0].payload["text"]
    assert len(web.ephemeral) == 1 and "Work paused" in web.ephemeral[0]["text"]
    receiver(socket, request)
    assert len(socket.acks) == 2 and "already received" in socket.acks[1].payload["text"]
    assert len(web.ephemeral) == 1


def test_pause_survives_service_restart(enrolled):
    settings, store, session_id, web = enrolled
    assert "Work paused" in command(settings, store, web, f"pause {session_id[-6:]}")
    store.close()
    reopened = MessageStore(settings.db_path)
    reopened.channels.configure(settings.channels)
    try:
        assert reopened.participation.self_presence(session_id, SECRET)["work_paused"] is True
        assert "Work resumed" in command(settings, reopened, web, f"resume {session_id[-6:]}")
    finally:
        reopened.close()


def test_identity_fragments_are_unique_current_channel_targets(enrolled):
    settings, store, session_id, web = enrolled
    for reference in ('flower', 'FLOWER', 'agentbus:flower'):
        assert resolve_session(store, reference, settings.slack_channel) == session_id
    assert 'Delivered' in command(settings, store, web, 'send flower Please report')
    assert 'Work paused' in command(settings, store, web, 'pause flow')
    assert 'Checkpoint requested' in command(settings, store, web, 'checkpoint FLOWER')
    assert 'Participation:' in command(settings, store, web, 'status agentbus:flower')


def test_ambiguous_identity_fragment_has_no_message_or_control_effect(enrolled):
    settings, store, session_id, web = enrolled
    secret = SECRET + '-bud'
    _, other, revision = store.participation.enroll(
        repo='agentbus', route='agentbus:flower-bud', display_name='Bud', session_secret=secret)
    store.participation.acknowledge_policy(other, secret, revision)
    for action in ('send flower inspect', 'wake flower inspect', 'pause flower', 'checkpoint flower', 'retire flower CONFIRM'):
        assert 'ambiguous' in command(settings, store, web, action)
    assert web.posts == []
    assert store.controls.deliver(session_id, SECRET) == []
    assert store.controls.deliver(other, secret) == []
    assert resolve_session(store, 'agentbus:flower', settings.slack_channel) == session_id


def test_identity_resolution_filters_foreign_sessions_before_presence(enrolled, monkeypatch):
    settings, store, session_id, web = enrolled
    store.channels.configure((settings.slack_channel, 'CFOREIGN'))
    secret = SECRET + '-foreign'
    grant = store.channels.issue_grant(
        channel='CFOREIGN', route='agentbus:flower-foreign', repo='agentbus', actor='operator')
    _, foreign, revision = store.channels.enroll(grant, repo='agentbus', route='agentbus:flower-foreign',
                                               display_name='Foreign', session_secret=secret)
    store.participation.acknowledge_policy(foreign, secret, revision)
    original = store.participation.presence

    def scoped_presence(candidate, **kwargs):
        assert candidate != foreign, 'foreign presence must not be queried during scoped resolution'
        return original(candidate, **kwargs)

    monkeypatch.setattr(store.participation, 'presence', scoped_presence)
    assert resolve_session(store, 'flower', settings.slack_channel) == session_id
    for reference in (foreign, foreign[-6:], 'foreign'):
        with pytest.raises(KeyError):
            resolve_session(store, reference, settings.slack_channel)
    assert web.posts == []


def test_old_alias_and_invalid_reference_cannot_select_a_session(enrolled):
    settings, store, session_id, web = enrolled
    store.participation.rename(session_id, SECRET, 'agentbus:blossom')
    assert resolve_session(store, 'blossom', settings.slack_channel) == session_id
    with pytest.raises(KeyError):
        resolve_session(store, 'flower', settings.slack_channel)
    for reference in ('%', '_', 'a' * 81, '<@U123>'):
        with pytest.raises(ValueError):
            resolve_session(store, reference, settings.slack_channel)


def receive_reply(settings, store, web, thread, text='human reply', *, user='U123', ts='1800000000.000001'):
    from src.agentbus_channel_ingress_service import append_scoped
    from src.agentbus_service import normalize_event
    _, message = normalize_event({'type':'message','channel':settings.slack_channel,
                                  'user':user,'ts':ts,'thread_ts':thread,'text':text},
                                 settings.slack_channel, 'BLOCAL')
    return append_scoped(store, settings.slack_channel, ts, message,
                         operator_user_ids=settings.slack_operator_user_ids)


def test_regular_reply_to_slash_send_has_identical_actionable_authority(enrolled):
    settings, store, session_id, web = enrolled
    assert 'Delivered' in command(settings, store, web, f'send {session_id} request root')
    root = store.read(settings.slack_channel).messages[-1]
    reply = receive_reply(settings, store, web, root.slack_ts)
    assert (reply.recipient, reply.kind, reply.audience, reply.sender_assurance) == (
        root.recipient, root.kind, root.audience, root.sender_assurance)
    assert reply.reply_to_cursor == root.cursor
    assert store.inbox(settings.slack_channel, 'agentbus:flower').messages[-1].actionable
    denied = receive_reply(settings, store, web, root.slack_ts, user='U999',ts='1800000000.000002')
    assert denied.audience == 'unrouted'
    assert denied.sender_assurance == 'slack-human'


def test_thread_reply_targets_one_proved_agent_and_preserves_ambiguity(enrolled):
    from src.agentbus_service import SendMessage
    settings, store, _, web = enrolled
    root = store.append(settings.slack_channel, '1700000000.010000',
                        SendMessage(sender='slack:U123', text='question', audience='unrouted'))
    answer = SendMessage(sender='agentbus:flower',text='answer',thread_ts=root.slack_ts)
    answer._sender_assurance='session'
    store.append(settings.slack_channel, '1700000000.010001', answer)
    assert receive_reply(settings, store, web, root.slack_ts).recipient == 'agentbus:flower'
    second_secret=SECRET+'-second'
    _, other, revision=store.participation.enroll(repo='agentbus',route='agentbus:other',display_name='Other',session_secret=second_secret)
    store.participation.acknowledge_policy(other,second_secret,revision)
    another=SendMessage(sender='agentbus:other',text='joined',thread_ts=root.slack_ts)
    another._sender_assurance='session'
    store.append(settings.slack_channel,'1700000000.010002',another)
    assert receive_reply(settings,store,web,root.slack_ts,ts='1800000000.000003').audience=='unrouted'


def test_regular_reply_reuses_send_quota_and_duplicate_does_not_spend_twice(enrolled,monkeypatch):
    settings,store,session_id,web=enrolled
    monkeypatch.setattr(store.send_rate,'MAX_SENDS',2)
    assert 'Delivered' in command(settings,store,web,f'send {session_id} root')
    root=store.read(settings.slack_channel).messages[-1]
    first=receive_reply(settings,store,web,root.slack_ts)
    assert first.audience=='direct'
    assert receive_reply(settings,store,web,root.slack_ts).cursor==first.cursor
    limited=receive_reply(settings,store,web,root.slack_ts,ts='1800000000.000004')
    assert limited.audience=='unrouted'
    assert 'rate exceeded' in command(settings,store,web,f'send {session_id} beyond quota')
    monkeypatch.setattr(store.send_rate,'MAX_SENDS',30)
    assert receive_reply(settings,store,web,root.slack_ts,ts='1800000000.000004').audience=='unrouted'


def test_retired_or_foreign_thread_target_stays_notification_only(enrolled):
    settings,store,session_id,web=enrolled
    assert 'Delivered' in command(settings,store,web,f'send {session_id} root')
    root=store.read(settings.slack_channel).messages[-1]
    assert 'Durable stop' in command(settings,store,web,f'retire {session_id} CONFIRM')
    controls=store.controls.deliver(session_id,SECRET)
    store.controls.acknowledge_stop(controls[0]['control_id'],session_id,SECRET)
    assert receive_reply(settings,store,web,root.slack_ts).audience=='unrouted'


def test_health_is_private_operator_only_and_does_not_need_a_target(enrolled):
    settings,store,_,web=enrolled
    assert 'not enabled' in command(settings,store,web,'health',user='U999')
    assert 'configured channel' in command(settings,store,web,'health',channel='CUNKNOWN')
    result=command(settings,store,web,'health')
    for word in ('AgentBus health','Version','Python','Channels','Federation','C123456'):
        assert word in result
    assert '🟡 unknown' in result.lower()  # A direct unit call cannot observe a socket.
    assert '*Channels:* `1` enabled' in result
    assert '*Storage:* `' in result
    assert 'protocol `2`' in result
    assert settings.slack_bot_token not in result and str(settings.db_path) not in result
    assert web.posts==[]


def test_health_socket_response_uses_rich_blocks_without_public_post(enrolled):
    settings,store,_,web=enrolled
    receiver=SlackReceiver(store,settings.channels,'B123',settings)
    socket=FakeSocket(web)
    request=SocketModeRequest(type='slash_commands',envelope_id='health-1',payload={
        'command':'/agentbus','channel_id':settings.slack_channel,'user_id':'U123',
        'trigger_id':'health-trigger','text':'health'})
    receiver(socket,request)
    receiver._commands.shutdown(wait=True)
    assert len(web.ephemeral)==1 and not web.posts
    assert web.ephemeral[0]['channel']==settings.slack_channel
    assert web.ephemeral[0]['user']=='U123'
    assert web.ephemeral[0]['blocks'][0]['type']=='header'
    assert any(b.get('text',{}).get('type')=='mrkdwn' for b in web.ephemeral[0]['blocks'])


def test_rich_message_projection_escapes_mentions_preserves_machine_carrier(enrolled):
    import json
    settings,store,session_id,web=enrolled
    text='*bold* and `code` <@U999> <!channel> <https://example.com|look> & more'
    assert 'Delivered' in command(settings,store,web,f'send {session_id} {text}')
    posted=web.posts[-1]
    assert json.loads(posted['text'])['text']==text
    body=''.join(b['text']['text'] for b in posted['blocks'] if b['type']=='section')
    assert '*bold*' in body and '`code`' in body
    assert '<@' not in body and '<!' not in body
    assert '&lt;@U999&gt;' in body and '&amp;' in body
    assert all(b['text']['verbatim'] for b in posted['blocks'] if b['type']=='section')


def test_privileged_dashboard_lists_enabled_and_retained_channels_without_peer_health_claims(enrolled):
    from pathlib import Path
    from types import SimpleNamespace

    from src.agentbus_status_api_service import service_dashboard
    settings,store,_,_=enrolled
    store.channels.configure((settings.slack_channel,'CSECOND','CDISABLED'))
    store.channels.configure((settings.slack_channel,'CSECOND'))
    settings=replace(settings,slack_channel='',slack_channels=(settings.slack_channel,'CSECOND'),
                     federation_config=Path('/private/secret-config.json'))
    bridge=SimpleNamespace(channel='CSECOND',bindings={'peer-a':object()},private_key='never-display')
    before=store._db.total_changes
    result=service_dashboard(settings,store,bridge=bridge)
    assert {c['channel']:c['enabled'] for c in result['channels']}=={'C123456':True,'CSECOND':True,'CDISABLED':False}
    assert result['federation']['configured'] and result['federation']['initialized']
    assert result['federation']['origins']==['peer-a']
    assert result['federation']['remote_health']=='not_observed'
    assert store._db.total_changes==before
    assert 'secret-config' not in str(result) and 'never-display' not in str(result)


def test_rich_block_bounds_do_not_split_escaped_entities_or_change_locator():
    from html import unescape

    from src.agentbus_local_delivery_service import LOCATOR_PREFIX, _locator_key
    from src.agentbus_presentation_service import message_blocks, visible_text
    from src.agentbus_service import SendMessage
    message=SendMessage(sender='agentbus:flower',text=('&<>'*1900)+'\u202e')
    key='11111111-1111-4111-8111-111111111111'
    blocks=message_blocks(message,locator=LOCATOR_PREFIX+key)
    assert _locator_key(blocks)==key
    texts=[b['text']['text'] for b in blocks if b['type']=='section']
    assert all(len(text)<=3000 for text in texts)
    assert all(not text.endswith(('&','&a','&am','&amp','&l','&lt','&g','&gt')) for text in texts)
    assert ''.join(unescape(text) for text in texts)==visible_text(message.text)


def test_regular_reply_authorization_empty_allowlist_foreign_parent_and_atomic_failure(enrolled):
    import sqlite3

    from src.agentbus_service import SendMessage
    settings,store,session_id,web=enrolled
    assert 'Delivered' in command(settings,store,web,f'send {session_id} root')
    root=store.read(settings.slack_channel).messages[-1]
    disabled=replace(settings,slack_operator_user_ids=frozenset())
    assert receive_reply(disabled,store,web,root.slack_ts).audience=='unrouted'
    # A thread existing only in another channel cannot be resolved here.
    store.channels.configure((settings.slack_channel,'CSECOND'))
    other=SendMessage(sender='agentbus:flower',text='foreign-only')
    other._sender_assurance='session'
    store.append('CSECOND','1900000000.000001',other)
    assert receive_reply(settings,store,web,'1900000000.000001',ts='1800000000.000002').audience=='unrouted'
    used=store._db.execute('SELECT used FROM send_rate_buckets WHERE principal=?',('slack:U123',)).fetchone()[0]
    with store._db:
        store._db.execute("CREATE TRIGGER reject_human_reply BEFORE INSERT ON messages BEGIN SELECT RAISE(ABORT,'storage unavailable'); END")
    with pytest.raises(sqlite3.Error):
        receive_reply(settings,store,web,root.slack_ts,ts='1800000000.000003')
    assert store._db.execute('SELECT used FROM send_rate_buckets WHERE principal=?',('slack:U123',)).fetchone()[0]==used


def test_channel_broadcast_single_shared_control_no_message_fanout(enrolled):
    settings, store, sid, web = enrolled
    payload = {'command': '/agentbus', 'channel_id': 'C123456', 'user_id': 'U123',
               'text': 'broadcast Everyone read this', 'trigger_id': 'broadcast-trigger'}
    result = execute_command(payload, settings, store, web)
    assert '1 session' in result
    assert execute_command(payload, settings, store, web) == result
    assert not web.posts
    assert store._db.execute('SELECT count(*) FROM operator_controls').fetchone()[0] == 1
    assert store.controls.deliver(sid, SECRET)[0]['reason'] == 'Everyone read this'
    assert 'not enabled' in execute_command({**payload, 'user_id': 'U999'}, settings, store, web)
    assert 'configured channel' in execute_command({**payload, 'channel_id': 'CFOREIGN'}, settings, store, web)


def test_poll_snapshot_excludes_foreign_and_late_join_sessions(enrolled):
    settings, store, sid, web = enrolled
    settings = replace(settings, slack_channels=('C123456', 'C654321'))
    store.channels.configure(settings.channels)
    grant = store.channels.issue_grant(channel='C654321', route='agentbus:foreign', repo='agentbus', actor='operator')
    # Enrollment uses the same supported grant path as the live service.
    other = store.channels.enroll(grant, repo='agentbus', route='agentbus:foreign', display_name='Foreign',
        session_secret='foreign-secret-with-at-least-32-characters')
    payload = {'command': '/agentbus', 'channel_id': 'C123456', 'user_id': 'U123',
               'text': 'poll What is blocked?', 'trigger_id': 'poll-trigger'}
    result = execute_command(payload, settings, store, web)
    assert '1 session' in result
    late_secret = 'late-secret-with-at-least-32-characters'
    late_grant = store.channels.issue_grant(channel='C123456', route='agentbus:late', repo='agentbus', actor='operator')
    _, late, _ = store.channels.enroll(late_grant, repo='agentbus', route='agentbus:late',
                                      display_name='Late', session_secret=late_secret)
    assert execute_command(payload, settings, store, web) == result
    assert store.controls.deliver(late, late_secret) == []
    assert store.controls.deliver(other[1], 'foreign-secret-with-at-least-32-characters') == []
    assert store.controls.deliver(sid, SECRET)[0]['kind'] == 'poll'


def test_results_are_private_operator_and_channel_scoped(enrolled):
    settings, store, sid, web = enrolled
    cid, _ = store.controls.issue_auxiliary(kind='poll', routes=None, session_ids=[sid],
                                           reason='Ready?', actor='operator')
    store.controls.deliver(sid, SECRET)
    store.controls.acknowledge_control(cid, sid, SECRET, answer='waiting for CI')
    assert 'waiting for CI' in command(settings, store, web, f'results {cid}')
    assert 'not enabled' in command(settings, store, web, f'results {cid}', user='U999')
    settings = replace(settings, slack_channels=('C123456', 'C654321'))
    assert 'No announcement results' in command(settings, store, web, f'results {cid}', channel='C654321')
