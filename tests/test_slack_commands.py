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
        repo="agentbus", route="agentbus:other", display_name="Other", session_secret=SECRET)
    store.participation.acknowledge_policy(second, SECRET, revision)
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
    try:
        assert reopened.participation.self_presence(session_id, SECRET)["work_paused"] is True
        assert "Work resumed" in command(settings, reopened, web, f"resume {session_id[-6:]}")
    finally:
        reopened.close()
