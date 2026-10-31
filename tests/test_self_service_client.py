"""A first join adopts the service UUID and survives an uncertain response."""

from argparse import Namespace
from pathlib import Path

import pytest

from src import agentbus_client as client
from src import agentbus_codex_wake_client as wake
from src.agentbus_transport_client import ClientError


def test_first_join_stages_credential_then_adopts_service_identity(monkeypatch, tmp_path, capsys):
    values = {"AGENTBUS_URL": "http://example.invalid", "AGENTBUS_API_TOKEN": "test"}
    profile = {"identity": "agentbus:new-chat", "repo": "agentbus",
               "display_name": "New Chat", "chat_id": "old-local-uuid",
               "service_url": values["AGENTBUS_URL"]}
    attempts = []
    monkeypatch.setattr(client, "load_profile", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(client, "save_profile", lambda *_: None)
    monkeypatch.setattr(wake, "enroll_current", lambda *_: {"status": "workspace_disabled"})

    def enroll(_values, path, body, **_kwargs):
        assert path == "/v1/sessions"
        attempts.append(body.copy())
        if len(attempts) == 1:
            raise ClientError("response lost")
        return {"chat_id": "server-issued-uuid", "session_id": "session-one",
                "channel": "C123", "inbox_id": "inbox-one", "high_water_cursor": 0,
                "revision": "revision-one", "values": {
                    "initial_interval_seconds": 60, "backoff_factor": 2,
                    "max_interval_seconds": 1920,
                }, "sources": {}, "ack_required": True}

    monkeypatch.setattr(client, "api", enroll)
    grant = tmp_path / "enrollment-grant"
    grant.write_text("g" * 40)
    grant.chmod(0o600)
    ns = Namespace(command="join", identity="agentbus:new-chat", handoff_file=None, enrollment_file=str(grant))
    with pytest.raises(ClientError, match="response lost"):
        client._execute_command(ns, Path(tmp_path), values)
    assert profile["chat_id"] == "old-local-uuid"
    assert "handoff_token" not in profile["participation_pending"]
    assert client._execute_command(ns, Path(tmp_path), values) == 0
    assert attempts[0]["session_secret"] == attempts[1]["session_secret"]
    assert profile["chat_id"] == "server-issued-uuid"
    assert profile["participation"]["session_id"] == "session-one"
    assert "session_secret" not in capsys.readouterr().out


@pytest.mark.parametrize('service_url', [None, 'http://foreign.invalid'])
def test_file_grant_join_rejects_missing_or_foreign_service_before_staging(monkeypatch, tmp_path, service_url):
    profile = {'identity': 'agentbus:new-chat', 'repo': 'agentbus',
               'display_name': 'New Chat', 'chat_id': 'local-profile-uuid'}
    if service_url is not None:
        profile['service_url'] = service_url
    before = dict(profile)
    values = {'AGENTBUS_URL': 'http://example.invalid', 'AGENTBUS_API_TOKEN': 'test'}
    monkeypatch.setattr(client, 'load_profile', lambda *_args, **_kwargs: profile)
    effects = []
    monkeypatch.setattr(client, 'save_profile', lambda *_args, **_kwargs: effects.append('save'))
    monkeypatch.setattr(client, 'api', lambda *_args, **_kwargs: effects.append('network'))
    grant = tmp_path / 'enrollment-grant'
    grant.write_text('g' * 40)
    grant.chmod(0o600)
    ns = Namespace(command='join', identity='agentbus:new-chat', handoff_file=None, enrollment_file=str(grant))
    with pytest.raises(ClientError, match='Enrollment service differs'):
        client._execute_command(ns, Path(tmp_path), values)
    assert profile == before and effects == []
    assert 'participation_pending' not in profile


@pytest.mark.parametrize('code', ['enrollment_request_expired', None, 'untrusted-secret-text'])
def test_enrollment_recovery_guidance_uses_only_authenticated_expiry_code(code):
    from src.agentbus_enrollment_client import _submit_enrollment
    payload = {'approval_request_id': 'request-one', 'session_secret': 'private-staged-secret'}
    def denied(*_args, **_kwargs):
        raise ClientError('AgentBus returned HTTP 403.', status=403, recovery_code=code)
    with pytest.raises(ClientError) as failed:
        _submit_enrollment({}, payload, 'example:project:agent', denied)
    assert failed.value.status == 403
    assert ('--new-enrollment-request' in str(failed.value)) == (code == 'enrollment_request_expired')
    assert 'private-staged-secret' not in str(failed.value)
    assert 'untrusted-secret-text' not in str(failed.value)


def _announcement_setup(tmp_path, text='hello'):
    import json

    identity = 'example:agentbus:intro'
    values = {'AGENTBUS_URL': 'http://localhost:8766', '_AGENTBUS_DELIVERY_VERSION': '2',
              'AGENTBUS_CONSUMER_STATE_DIR': str(tmp_path / 'profiles'),
              'AGENTBUS_STATE_DIR': str(tmp_path / 'runtime')}
    path = client.identity_path(values, identity)
    profile = {'schema_version': 2, 'identity': identity, 'repo': 'agentbus', 'display_name': 'Intro',
               'chat_id': 'intro-chat', 'channel': 'C111', 'inbox_id': 'intro-inbox',
               'service_url': values['AGENTBUS_URL'], 'remit': 'keep useful receipts',
               'ack_cursor': 0, 'observed_cursor': 0, 'announcement_pending': text,
               'participation': {'session_id': 'intro-session', 'session_secret': 'private-intro-secret',
                                 'acknowledged_policy_revision': 'r1', 'policy_values': {}}}
    client.atomic_json(path, profile)
    info = {'features': ['message-lifecycle-v1'], 'lifecycle': {'version': 1, 'request_epoch': 'a' * 64}}
    def checked(*_args):
        return client.load_profile(values, identity, save_migration=False), info
    return values, path, json.loads(path.read_text()), info, checked


@pytest.mark.parametrize('text', ['hello', '-'])
def test_intro_stages_before_post_and_replays_original_epoch_after_lost_response(tmp_path, monkeypatch, text):
    import json
    import sys

    from src.agentbus_enrollment_client import announce_pending

    values, path, profile, info, checked = _announcement_setup(tmp_path, text)
    calls = []
    class NoStdin:
        def read(self):
            raise AssertionError('Introduction text is literal; never read stdin')
    monkeypatch.setattr(sys, 'stdin', NoStdin())
    def post(_values, route, body, **options):
        assert route == '/v1/messages' and body['text'] == text
        disk = json.loads(path.read_text())
        assert disk['announcement_message_id'] == options['delivery_key']
        stages = list((tmp_path / 'runtime/send-requests').glob('*.json'))
        assert len(stages) == 1
        stage = json.loads(stages[0].read_text())
        assert stage['request_id'] == options['delivery_key']
        assert stage['epoch'] == options['delivery_epoch'] == 'a' * 64
        assert stage['payload'] == body
        calls.append((body.copy(), options.copy()))
        if len(calls) == 1:
            raise ClientError('lost response')
        return {'cursor': 7, 'slack_ts': ''}
    with pytest.raises(ClientError, match='acceptance may be uncertain'):
        announce_pending(values, profile, post, checked)
    assert json.loads(path.read_text())['announcement_pending'] == text
    info['lifecycle']['request_epoch'] = 'b' * 64
    announce_pending(values, profile, post, checked)
    assert calls[0] == calls[1]
    disk = json.loads(path.read_text())
    assert disk['announcement'] == {'cursor': 7, 'slack_ts': ''}
    assert 'announcement_pending' not in disk and disk['ack_cursor'] == 0


def test_intro_missing_original_stage_never_replaces_request_or_posts(tmp_path):
    import json

    from src.agentbus_enrollment_client import announce_pending

    values, path, profile, _info, checked = _announcement_setup(tmp_path)
    profile['announcement_message_id'] = '7d76697d-ed3f-4abf-afc9-45e20312f42a'
    client.atomic_json(path, profile)
    before = path.read_bytes()
    calls = []
    with pytest.raises(ClientError, match='Original lifecycle request stage is missing'):
        announce_pending(values, profile, lambda *_a, **_k: calls.append('post'), checked)
    assert calls == [] and path.read_bytes() == before
    assert json.loads(path.read_text())['announcement_message_id'] == profile['announcement_message_id']


def test_intro_concurrent_first_submit_posts_only_winning_staged_identity(tmp_path):
    import json
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from src.agentbus_enrollment_client import announce_pending

    values, path, profile, info, _checked = _announcement_setup(tmp_path)
    barrier = threading.Barrier(2, timeout=5)
    calls = []
    def checked(*_args):
        current = client.load_profile(values, profile['identity'], save_migration=False)
        barrier.wait()
        return current, info
    def post(_values, _route, _body, **options):
        calls.append(options['delivery_key'])
        return {'cursor': 7, 'slack_ts': ''}
    def submit():
        try:
            announce_pending(values, profile, post, checked)
        except ClientError:
            return 'conflict'
        return 'accepted'
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(outcomes) == ['accepted', 'conflict']
    assert len(calls) == 1
    assert json.loads(path.read_text())['announcement_message_id'] == calls[0]
    assert len(list((tmp_path / 'runtime/send-requests').glob('*.json'))) == 2


def test_policy_ack_starts_worker_before_optional_intro_failure(tmp_path, monkeypatch, capsys):
    import json

    from src import agentbus_enrollment_client as enrollment
    from src import agentbus_poll_client as poll
    from src.agentbus_parser_client import parse_args

    values, path, _profile, _info, checked = _announcement_setup(tmp_path)
    effects = []
    monkeypatch.setattr(client, 'checked_profile', checked)
    monkeypatch.setattr(client, 'api', lambda *_a, **_k: {'receipt': 'policy-receipt'})
    monkeypatch.setattr(poll, 'ensure_worker', lambda *_a: effects.append('worker'))
    def failed_intro(*_args):
        effects.append('introduction')
        raise ClientError('introduction pending')
    monkeypatch.setattr(enrollment, 'announce_pending', failed_intro)
    ns = parse_args(['policy-ack', '--identity', 'example:agentbus:intro', 'r2'])
    with pytest.raises(ClientError, match='introduction pending'):
        client._execute_command(ns, tmp_path, values)
    assert effects == ['worker', 'introduction']
    disk = json.loads(path.read_text())
    assert disk['participation']['acknowledged_policy_revision'] == 'r2'
    assert disk['ack_cursor'] == 0
    assert capsys.readouterr().out == 'ACK policy-receipt POLICY\n'
