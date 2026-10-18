"""Launcher regressions using temporary state and local HTTP servers only."""

import io
import json
import os
import signal
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "agentbus"
TOKEN = "test-agentbus-token-never-use-in-production"


@pytest.mark.parametrize("channels", [
    {"AGENTBUS_SLACK_CHANNEL": "C111"},
    {"AGENTBUS_SLACK_CHANNELS": "C111,C222"},
])
def test_serve_accepts_either_channel_configuration(launcher, project, monkeypatch, channels):
    (project / "uv.lock").write_text("test-lock")
    values = {"AGENTBUS_API_TOKEN": TOKEN, "AGENTBUS_SLACK_BOT_TOKEN": "xoxb-test",
              "AGENTBUS_SLACK_APP_TOKEN": "xapp-test", "AGENTBUS_HOST": "127.0.0.1",
              "AGENTBUS_PORT": "8766", **channels}
    calls = []
    monkeypatch.setattr(launcher.os, "execvpe", lambda *args: calls.append(args))
    launcher.serve(project, values)
    assert len(calls) == 1
    assert calls[0][0] == "uv"
    assert calls[0][2] == values


@pytest.mark.parametrize("single", ["C111", ""])
def test_serve_rejects_simultaneous_channel_forms_before_execution(launcher, project, monkeypatch, single):
    (project / "uv.lock").write_text("test-lock")
    values = {"AGENTBUS_API_TOKEN": TOKEN, "AGENTBUS_SLACK_BOT_TOKEN": "xoxb-test",
              "AGENTBUS_SLACK_APP_TOKEN": "xapp-test", "AGENTBUS_SLACK_CHANNEL": single,
              "AGENTBUS_SLACK_CHANNELS": "C111,C222"}
    calls = []
    monkeypatch.setattr(launcher.os, "execvpe", lambda *args: calls.append(args))
    with pytest.raises(launcher.ClientError, match="both channel"):
        launcher.serve(project, values)
    assert calls == []


@pytest.mark.parametrize("command", ["start", "autostart"])
@pytest.mark.parametrize("channels", [
    {"AGENTBUS_SLACK_CHANNEL": "C111"},
    {"AGENTBUS_SLACK_CHANNELS": "C111,C222"},
])
def test_lifecycle_accepts_either_channel_configuration(launcher, project, monkeypatch, command, channels):
    values = {"AGENTBUS_API_TOKEN": TOKEN, "AGENTBUS_SLACK_BOT_TOKEN": "xoxb-test",
              "AGENTBUS_SLACK_APP_TOKEN": "xapp-test", "AGENTBUS_AUTOSTART": "1",
              "AGENTBUS_STATE_DIR": str(project / "state"), **channels}
    calls = []

    class Child:
        pid = 4321

        def poll(self):
            return None

    def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return Child()

    monkeypatch.setattr(launcher.subprocess, "Popen", spawn)
    monkeypatch.setattr(launcher, "running_process", lambda _state: None)
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: "test-start")
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    assert launcher.lifecycle(command, project, values) == 0
    assert len(calls) == 1
    assert calls[0][0][0][-1] == "serve"
    assert calls[0][1]["env"] == values


@pytest.mark.parametrize("command", ["start", "autostart"])
@pytest.mark.parametrize("single", ["C111", ""])
def test_lifecycle_rejects_simultaneous_channel_forms_before_spawn(launcher, project, monkeypatch, command, single):
    values = {"AGENTBUS_API_TOKEN": TOKEN, "AGENTBUS_SLACK_BOT_TOKEN": "xoxb-test",
              "AGENTBUS_SLACK_APP_TOKEN": "xapp-test", "AGENTBUS_AUTOSTART": "1",
              "AGENTBUS_STATE_DIR": str(project / "state"), "AGENTBUS_SLACK_CHANNEL": single,
              "AGENTBUS_SLACK_CHANNELS": "C111,C222"}
    monkeypatch.setattr(launcher, "running_process", lambda _state: None)

    def forbidden_spawn(*args, **kwargs):
        pytest.fail("Ambiguous channel configuration reached process creation")

    monkeypatch.setattr(launcher.subprocess, "Popen", forbidden_spawn)
    with pytest.raises(launcher.ClientError, match="both channel"):
        launcher.lifecycle(command, project, values)


@pytest.mark.parametrize("command", ["serve", "start", "autostart"])
def test_all_service_entrypoints_require_a_channel_before_execution(launcher, project, monkeypatch, command):
    values = {"AGENTBUS_API_TOKEN": TOKEN, "AGENTBUS_SLACK_BOT_TOKEN": "xoxb-test",
              "AGENTBUS_SLACK_APP_TOKEN": "xapp-test", "AGENTBUS_AUTOSTART": "1",
              "AGENTBUS_STATE_DIR": str(project / "state")}
    monkeypatch.setattr(launcher, "running_process", lambda _state: None)
    with pytest.raises(launcher.ClientError, match="SLACK_CHANNEL"):
        if command == "serve":
            launcher.serve(project, values)
        else:
            launcher.lifecycle(command, project, values)


@pytest.fixture
def launcher():
    loader = SourceFileLoader("agentbus_launcher_tests", str(LAUNCHER))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    original_umask = os.umask(0o077)
    os.umask(original_umask)
    try:
        yield module
    finally:
        os.umask(original_umask)


@pytest.fixture
def project(tmp_path, monkeypatch):
    for key in os.environ:
        if key.startswith("AGENTBUS_"):
            monkeypatch.delenv(key)
    root = tmp_path / "project with spaces"
    root.mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'test'\n")
    monkeypatch.setenv("AGENTBUS_PROJECT_DIR", str(root))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    return root


@contextmanager
def http_service(responder):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            request = {"method": self.command, "path": self.path,
                       "authorization": self.headers.get("Authorization"),
                       "capabilities": self.headers.get("X-AgentBus-Client-Capabilities"),
                       "body": body}
            received.append(request)
            status, headers, payload = responder(request)
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def scope_info():
    return {"inbox_id": "inbox-1", "channel": "C123", "chat_id": "chat-1",
            "session_id": "session-1", "high_water_cursor": 50,
            "features": ["session-required", "channel-isolation-v1", "channel-local-cursors-v1"]}


def joined_profile():
    return {"repo": "tools", "identity": "tools:weed", "chat_id": "chat-1",
            "channel": "C123", "inbox_id": "inbox-1", "service_url": "http://127.0.0.1:8766",
            "participation": {"session_id": "session-1", "session_secret": "s" * 32}}


def success(_request):
    return 200, {"Content-Type": "application/json"}, b'{"ok":true}'


def test_configuration_treats_shell_syntax_as_data_and_environment_wins(launcher, project, monkeypatch):
    marker = project / "must-not-be-created"
    literal = f"$(touch {marker}) `touch {marker}`"
    (project / ".env").write_text(
        f"export AGENTBUS_API_TOKEN='{literal}' # literal, never executed\n"
        "AGENTBUS_SLACK_CHANNEL=C_CONFIG\nAGENTBUS_PORT=9876\n"
    )
    monkeypatch.setenv("AGENTBUS_SLACK_CHANNEL", "C_ENVIRONMENT")
    actual_project, values = launcher.configuration()
    assert actual_project == project
    assert values["AGENTBUS_API_TOKEN"] == literal
    assert values["AGENTBUS_SLACK_CHANNEL"] == "C_ENVIRONMENT"
    assert values["AGENTBUS_URL"] == "http://127.0.0.1:9876"
    assert Path(values["AGENTBUS_DB_PATH"]).parent == project.parent / "xdg-state/agentbus"
    assert not marker.exists()


def test_configuration_keeps_existing_checkout_database_location(launcher, project):
    (project / ".state").mkdir()
    _, values = launcher.configuration()
    assert Path(values["AGENTBUS_DB_PATH"]) == project / ".state/messages.sqlite3"


def test_codex_wake_detached_worker_receives_resolved_env_settings(launcher, project, tmp_path):
    from src import agentbus_codex_wake_client as wake

    marker = tmp_path / "detached-worker-env.json"
    (project / ".env").write_text(
        "AGENTBUS_CODEX_HOST_MODE=standalone\n"
        "AGENTBUS_CODEX_TURN_MAX_SECONDS=90\n"
        f'AGENTBUS_WAKE_TEST_MARKER="{marker}"\n'
    )
    (project / "agentbus").write_text('''
import json, os, pathlib, time
path = pathlib.Path(os.environ["AGENTBUS_WAKE_TEST_MARKER"])
path.write_text(json.dumps({
    "mode": os.environ.get("AGENTBUS_CODEX_HOST_MODE"),
    "timeout": os.environ.get("AGENTBUS_CODEX_TURN_MAX_SECONDS"),
}))
while True:
    time.sleep(1)
''')
    actual_project, values = launcher.configuration()
    assert actual_project == project
    started = wake.start_worker(project, values)
    try:
        assert started["already_running"] is False
        deadline = time.monotonic() + 3
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists(), "detached worker did not start"
        assert json.loads(marker.read_text()) == {"mode": "standalone", "timeout": "90"}
    finally:
        wake.stop_worker(values)


def test_operator_capability_is_not_loaded_into_ordinary_client_configuration(launcher, project, monkeypatch):
    (project / ".env").write_text("AGENTBUS_OPERATOR_TOKEN=service-only-secret\n")
    monkeypatch.setenv("AGENTBUS_OPERATOR_TOKEN", "explicit-operator-secret")
    _, ordinary = launcher.configuration()
    _, service = launcher.configuration(include_operator=True)
    assert "AGENTBUS_OPERATOR_TOKEN" not in ordinary
    assert service["AGENTBUS_OPERATOR_TOKEN"] == "explicit-operator-secret"


@pytest.mark.parametrize("line", [f"NOT_AGENTBUS={TOKEN}", f"AGENTBUS_API_TOKEN='{TOKEN}"])
def test_invalid_config_error_does_not_echo_values(launcher, project, line):
    (project / ".env").write_text(line)
    with pytest.raises(launcher.ClientError) as exc:
        launcher.configuration()
    assert TOKEN not in str(exc.value)


def test_api_post_preserves_body_and_uses_bearer_header(launcher):
    with http_service(success) as (base, received):
        payload = {"sender": "test", "text": "quotes ' \"\nemoji 🧑‍💻 and $(data)"}
        assert launcher.api({"AGENTBUS_URL": base, "AGENTBUS_API_TOKEN": TOKEN},
                            "/v1/messages", payload) == {"ok": True}
    assert len(received) == 1
    assert received[0]["method"] == "POST"
    assert received[0]["authorization"] == f"Bearer {TOKEN}"
    assert received[0]["capabilities"] == "participation-v1"
    assert json.loads(received[0]["body"]) == payload
    assert TOKEN not in received[0]["path"]


def test_api_requires_token_before_connecting(launcher):
    with http_service(success) as (base, received):
        with pytest.raises(launcher.ClientError, match="AGENTBUS_API_TOKEN"):
            launcher.api({"AGENTBUS_URL": base}, "/v1/messages")
        assert received == []


def test_health_check_omits_credentials(launcher):
    with http_service(success) as (base, received):
        assert launcher.api({"AGENTBUS_URL": base, "AGENTBUS_API_TOKEN": TOKEN},
                            "/healthz") == {"ok": True}
    assert received[0]["authorization"] is None


@pytest.mark.parametrize("base", [
    "http://example.invalid",
    "ftp://127.0.0.1",
    "https://user:secret@example.invalid",
    "http://127.0.0.1?wrong=endpoint",
    "http://127.0.0.1#ignored-route",
    "http://127.0.0.1:invalid",
])
def test_api_rejects_unsafe_configured_urls_before_connecting(launcher, base):
    with pytest.raises(launcher.ClientError):
        launcher.api({"AGENTBUS_URL": base, "AGENTBUS_API_TOKEN": TOKEN}, "/v1/messages")


def test_api_does_not_follow_redirects_with_bearer_token(launcher):
    with http_service(success) as (target, leaked):
        def redirect(_request):
            return 302, {"Location": target + "/credential-capture"}, b""

        with http_service(redirect) as (base, received):
            with pytest.raises(launcher.ClientError):
                launcher.api({"AGENTBUS_URL": base, "AGENTBUS_API_TOKEN": TOKEN}, "/v1/messages")
            assert len(received) == 1
        assert leaked == []


def test_api_error_reports_retry_delay_without_echoing_response_body(launcher):
    def limited(_request):
        return 429, {"Retry-After": "7"}, TOKEN.encode()

    with http_service(limited) as (base, _received):
        with pytest.raises(launcher.ClientError) as exc:
            launcher.api({"AGENTBUS_URL": base, "AGENTBUS_API_TOKEN": TOKEN}, "/v1/messages")
    assert "429" in str(exc.value)
    assert "7" in str(exc.value)
    assert TOKEN not in str(exc.value)


def test_invalid_json_response_fails_without_echoing_body(launcher):
    def non_json(_request):
        return 200, {"Content-Type": "text/html"}, f"<html>{TOKEN}</html>".encode()

    with http_service(non_json) as (base, _received):
        with pytest.raises(launcher.ClientError) as exc:
            launcher.api({"AGENTBUS_URL": base, "AGENTBUS_API_TOKEN": TOKEN}, "/v1/messages")
    assert TOKEN not in str(exc.value)


@pytest.mark.parametrize("recorded, observed, expected", [
    ("100", "100", 4321),
    ("100", "200", None),
    ("100", None, None),
    (None, None, None),
])
def test_running_process_requires_live_matching_start_identity(launcher, tmp_path, monkeypatch,
                                                             recorded, observed, expected):
    (tmp_path / "service.json").write_text(json.dumps({"pid": 4321, "start_time": recorded}))
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: observed)
    assert launcher.running_process(tmp_path) == expected


def test_stop_never_signals_reused_pid(launcher, project, tmp_path, monkeypatch):
    (tmp_path / "service.json").write_text(json.dumps({"pid": 4321, "start_time": "old"}))
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: "new")
    signals = []
    monkeypatch.setattr(launcher.os, "killpg", lambda *args: signals.append(args))
    assert launcher.lifecycle("stop", project, {"AGENTBUS_STATE_DIR": str(tmp_path)}) == 0
    assert signals == []


def test_stop_signals_service_group_and_removes_exited_record(launcher, project, tmp_path, monkeypatch):
    record = tmp_path / "service.json"
    record.write_text(json.dumps({"pid": 4321, "start_time": "100"}))
    alive = [True]
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: "100" if alive[0] else None)
    signals = []

    def terminate(*args):
        signals.append(args)
        alive[0] = False

    monkeypatch.setattr(launcher.os, "killpg", terminate)
    assert launcher.lifecycle("stop", project, {"AGENTBUS_STATE_DIR": str(tmp_path)}) == 0
    assert signals == [(4321, signal.SIGTERM)]
    assert not record.exists()


def test_disabled_autostart_needs_no_credentials_or_state(launcher, project):
    state = project / "unused-state"
    assert launcher.lifecycle("autostart", project,
                              {"AGENTBUS_STATE_DIR": str(state), "AGENTBUS_AUTOSTART": "0"}) == 0
    assert not state.exists()


def test_start_does_not_spawn_second_receiver_when_service_is_running(launcher, project, tmp_path, monkeypatch):
    (tmp_path / "service.json").write_text(json.dumps({"pid": 4321, "start_time": "100"}))
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: "100")
    spawned = []
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    assert launcher.lifecycle("start", project, {"AGENTBUS_STATE_DIR": str(tmp_path)}) == 0
    assert spawned == []


def test_health_status_does_not_hold_lock_needed_to_stop_service(launcher, project, tmp_path, monkeypatch):
    def health(_values, path):
        assert path == "/v1/status"
        with (tmp_path / "service.lock").open("a") as other_command:
            launcher.fcntl.flock(other_command, launcher.fcntl.LOCK_EX | launcher.fcntl.LOCK_NB)
        return {"status": "ok", "slack_connected": False}

    monkeypatch.setattr(launcher, "api", health)
    assert launcher.lifecycle("status", project, {"AGENTBUS_STATE_DIR": str(tmp_path)}) == 0


def test_active_poll_check_returns_silently_when_worker_has_no_event(launcher, tmp_path, monkeypatch, capsys):
    from src import agentbus_poll_client as poll

    profile = {"identity": "agentbus:flower", "participation": {"session_id": "s1", "stopped": False}}
    path = tmp_path / "agentbus:flower.json"
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(launcher, "configuration", lambda **_kwargs: (tmp_path, {}))
    monkeypatch.setattr(launcher, "checked_profile", lambda _values, _identity: (profile, {}))
    monkeypatch.setattr(launcher, "identity_path", lambda _values, _identity: path)
    monkeypatch.setattr(poll, "ensure_worker", lambda *_args: None)
    monkeypatch.setattr(poll, "peek_event", lambda *_args, **_kwargs: None)
    assert launcher.main(["poll", "--check", "--identity", "agentbus:flower"]) == 0
    assert capsys.readouterr().out == ""


def test_failed_start_removes_pid_record_and_keeps_credentials_out_of_argv(launcher, project, tmp_path, monkeypatch):
    values = {"AGENTBUS_STATE_DIR": str(tmp_path), "AGENTBUS_API_TOKEN": TOKEN,
              "AGENTBUS_SLACK_BOT_TOKEN": "xoxb-test", "AGENTBUS_SLACK_APP_TOKEN": "xapp-test",
              "AGENTBUS_SLACK_CHANNEL": "C123"}
    calls = []

    class FailedChild:
        pid = 4321

        def poll(self):
            return 1

    def popen(args, **kwargs):
        calls.append((args, kwargs))
        return FailedChild()

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: "100")
    with pytest.raises(launcher.ClientError, match="failed to start"):
        launcher.lifecycle("start", project, values)
    assert not (tmp_path / "service.json").exists()
    assert TOKEN not in " ".join(calls[0][0])
    assert calls[0][1]["env"]["AGENTBUS_API_TOKEN"] == TOKEN
    assert calls[0][1]["start_new_session"] is True


def test_send_reads_stdin_and_preserves_thread_and_recipient(launcher, monkeypatch, capsys):
    captured = []
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), {}))
    monkeypatch.setattr(launcher, "api", lambda values, path, payload=None, **_kwargs: captured.append((path, payload)) or {"ok": True})
    monkeypatch.setattr(launcher.sys, "stdin", io.StringIO("first line\nsecond line\n"))
    monkeypatch.setattr(launcher, "checked_profile", lambda *_args: (joined_profile(), {}))
    assert launcher.main(["send", "-", "--sender", "writer", "--recipient", "reviewer",
                          "--thread-ts", "123.456", "--correlation-id", "task-1"]) == 0
    assert captured == [("/v1/messages", {
        "text": "first line\nsecond line\n", "sender": "writer", "recipient": "reviewer", "repo": "tools",
        "kind": "message", "audience": "direct", "thread_ts": "123.456", "correlation_id": "task-1",
    })]
    assert json.loads(capsys.readouterr().out) == {"ok": True}


def test_read_encodes_filter_query_without_leaking_token(launcher, monkeypatch):
    captured = []
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), {"AGENTBUS_API_TOKEN": TOKEN, "AGENTBUS_URL": "http://127.0.0.1:8766"}))
    monkeypatch.setattr(launcher, "checked_profile", lambda *_args: (joined_profile(), {}))
    monkeypatch.setattr(launcher, "api", lambda values, path, payload=None, **_kwargs: captured.append(path) or {"messages": []})
    assert launcher.main(["read", "--identity", "tools:weed", "--after", "17", "--limit", "3", "--recipient", "agent:one",
                          "--thread-ts", "123.456"]) == 0
    parsed = urlsplit(captured[0])
    assert parsed.path == "/v1/messages"
    assert parse_qs(parsed.query) == {"after": ["17"], "limit": ["3"],
                                    "recipient": ["agent:one"], "thread_ts": ["123.456"]}
    assert TOKEN not in captured[0]


def test_onboard_creates_unique_chat_profiles_for_same_repo(launcher, tmp_path, monkeypatch, capsys):
    state = tmp_path / "consumers"
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766",
              "AGENTBUS_LOCAL_PREFIX": "home"}
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), values))
    monkeypatch.setattr(launcher, "api", lambda _values, path, payload=None: {
        "protocol_version": 2, "inbox_id": "inbox-1", "channel": "C123", "high_water_cursor": 42,
    })
    for name in ("weed", "fern"):
        assert launcher.main(["onboard", "--repo", "tools", "--name", name,
                              "--role", "maintainer"]) == 0
    weed = json.loads((state / "home:tools:weed.json").read_text())
    fern = json.loads((state / "home:tools:fern.json").read_text())
    assert weed["identity"] == "home:tools:weed" and fern["identity"] == "home:tools:fern"
    assert weed["chat_id"] != fern["chat_id"]
    assert weed["ack_cursor"] == fern["ack_cursor"] == 0
    assert weed["inbox_id"] is None and fern["channel"] is None
    assert (state.stat().st_mode & 0o777) == 0o700
    assert (state / "home:tools:weed.json").stat().st_mode & 0o777 == 0o600
    assert launcher.main(["onboard", "--repo", "tools", "--name", "weed",
                          "--role", "maintainer"]) == 1
    assert "already exists" in capsys.readouterr().err


def test_onboard_requires_valid_local_prefix_before_service_or_profile_write(
    launcher, tmp_path, monkeypatch, capsys,
):
    state = tmp_path / "consumers"
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766"}
    monkeypatch.setattr(launcher, "configuration", lambda **_kwargs: (Path("/unused"), values))
    monkeypatch.setattr(launcher, "api", lambda *_args, **_kwargs:
                        pytest.fail("invalid prefix reached the service"))
    command = ["onboard", "--repo", "tools", "--name", "weed", "--role", "maintainer"]
    for prefix in (None, "Home", "slack", "bad:prefix", "x" * 33):
        if prefix is None:
            values.pop("AGENTBUS_LOCAL_PREFIX", None)
        else:
            values["AGENTBUS_LOCAL_PREFIX"] = prefix
        assert launcher.main(command) == 1
        assert "AGENTBUS_LOCAL_PREFIX" in capsys.readouterr().err
        assert not state.exists()


def test_inbox_after_zero_is_stateless_and_ack_is_bounded(launcher, tmp_path, monkeypatch, capsys):
    state = tmp_path / "consumers"
    state.mkdir()
    profile = {"schema_version": 2, "chat_id": "chat-1", "identity": "tools:weed",
               "repo": "tools", "name": "weed", "display_name": "Weed", "role": "maintainer",
               "voice": "plain", "remit": "tools", "inbox_id": "inbox-1", "channel": "C123",
               "service_url": "http://127.0.0.1:8766", "ack_cursor": 8, "observed_cursor": 10,
               "participation": {"session_id": "session-1", "session_secret": "s" * 32}}
    (state / "tools:weed.json").write_text(json.dumps(profile))
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766"}
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), values))

    def fake_api(_values, path, payload=None, **_kwargs):
        if path == "/v1/info":
            return scope_info()
        assert "after=0" in path
        return {"messages": [{"cursor": 40}], "next_cursor": 50, "has_more": False}

    monkeypatch.setattr(launcher, "api", fake_api)
    assert launcher.main(["inbox", "--identity", "tools:weed", "--after", "0"]) == 0
    assert json.loads((state / "tools:weed.json").read_text())["observed_cursor"] == 10
    capsys.readouterr()
    assert launcher.main(["ack", "--identity", "tools:weed", "--through", "11"]) == 1
    assert "highest cursor observed" in capsys.readouterr().err
    assert launcher.main(["ack", "--identity", "tools:weed", "--through", "10"]) == 0
    assert json.loads((state / "tools:weed.json").read_text())["ack_cursor"] == 10


def test_cli_rejects_ambiguous_actionable_send_before_api(launcher, monkeypatch, capsys):
    called = []
    monkeypatch.setattr(launcher, "checked_profile", lambda *_args: (joined_profile(), {}))
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), {}))
    monkeypatch.setattr(launcher, "api", lambda *args, **kwargs: called.append(args))
    assert launcher.main(["send", "Who owns this?", "--sender", "tools:weed",
                          "--kind", "question"]) == 1
    assert called == []
    assert "requires --to" in capsys.readouterr().err
    for args in (
        ["send", "A status note", "--sender", "tools:weed"],
        ["send", "A status note", "--sender", "tools:weed", "--informational"],
        ["send", "A reply", "--sender", "tools:weed", "--recipient", "bcf:thistle",
         "--reply-to-cursor", "42"],
    ):
        assert launcher.main(args) == 1
    assert called == []


def test_cli_reply_sends_only_parent_cursor_and_text(launcher, monkeypatch, capsys):
    payloads = []
    profile = {**joined_profile(), "repo": "agentbus"}
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), {}))
    monkeypatch.setattr(launcher, "checked_profile", lambda *_args: (profile, {}))
    monkeypatch.setattr(launcher, "api", lambda _values, path, payload=None, **_kwargs:
                        payloads.append((path, payload)) or {"cursor": 43})
    assert launcher.main(["reply", "Seen", "--identity", "agentbus:flower", "--to-cursor", "42"]) == 0
    assert payloads == [("/v1/messages", {
        "sender": "agentbus:flower", "text": "Seen", "repo": "agentbus",
        "kind": "reply", "reply_to_cursor": 42,
    })]
    assert json.loads(capsys.readouterr().out)["cursor"] == 43


def test_consumer_profiles_reject_symlinked_state(launcher, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(outside, target_is_directory=True)
    with pytest.raises(launcher.ClientError, match="unsafe"):
        launcher.consumer_state_dir({"AGENTBUS_CONSUMER_STATE_DIR": str(linked)})


def test_consumer_profiles_use_portable_xdg_default_and_explicit_migration_path(
    launcher, tmp_path,
):
    xdg = tmp_path / "xdg-state"
    assert launcher.consumer_state_dir({"XDG_STATE_HOME": str(xdg)}) == \
        xdg / "agentbus/consumers"
    explicit = tmp_path / "explicit"
    assert launcher.consumer_state_dir({
        "XDG_STATE_HOME": str(xdg),
        "AGENTBUS_CONSUMER_STATE_DIR": str(explicit),
    }) == explicit
    explicit.mkdir()
    (explicit / "tools.json").write_text(json.dumps({
        "schema_version": 2, "identity": "tools:fern", "chat_id": "stable-chat",
    }))
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(explicit)}
    assert launcher.load_profile(values, "tools:fern")["chat_id"] == "stable-chat"
    with pytest.raises(launcher.ClientError, match="Unknown AgentBus identity"):
        launcher.load_profile(values, "tools:other")


def test_onboard_accepts_realistic_repo_names(launcher, tmp_path, monkeypatch):
    state = tmp_path / "state"
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766",
              "AGENTBUS_LOCAL_PREFIX": "home"}
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), values))
    monkeypatch.setattr(launcher, "api", lambda *_args, **_kwargs: {
        "inbox_id": "inbox-1", "channel": "C123", "high_water_cursor": 9,
    })
    assert launcher.main(["onboard", "--repo", "economic_data", "--name", "fern",
                          "--role", "analyst"]) == 0
    assert (state / "home:economic_data:fern.json").is_file()
    long_repo = "bcf-protection-inspector-activation-prospective"
    assert launcher.main(["onboard", "--repo", long_repo, "--name", "moss",
                          "--role", "reviewer"]) == 0
    assert (state / f"home:{long_repo}:moss.json").is_file()


def test_onboard_derives_repo_and_records_rich_persona(launcher, tmp_path, monkeypatch):
    state = tmp_path / "state"
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766",
              "AGENTBUS_LOCAL_PREFIX": "home"}
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), values))
    from src import agentbus_client
    monkeypatch.setattr(agentbus_client, "current_repo", lambda: "some_repo")
    monkeypatch.setattr(launcher, "api", lambda *_args, **_kwargs: {
        "inbox_id": "inbox-1", "channel": "C123", "high_water_cursor": 9,
    })
    assert launcher.main([
        "onboard", "--name", "firefly", "--role", "integration-scout",
        "--display-name", "Firefly", "--voice", "wry, observant, and concise",
        "--remit", "Trace boundaries and make handoffs legible",
        "--values", "curiosity, evidence, and kindness",
        "--working-style", "map the terrain, test assumptions, then leave a clear trail",
        "--signature", "finds the one loose wire in a dark machine room",
    ]) == 0
    profile = json.loads((state / "home:some_repo:firefly.json").read_text())
    assert profile.items() >= {
        "display_name": "Firefly", "role": "integration-scout",
        "values": "curiosity, evidence, and kindness",
        "working_style": "map the terrain, test assumptions, then leave a clear trail",
        "signature": "finds the one loose wire in a dark machine room",
    }.items()


def test_public_cli_operation_inventory_matches_parser_surface(launcher):
    import argparse

    from src.agentbus_parser_client import build_parser

    assert set(launcher.CLI_OPERATIONS) == {
        "serve", "start", "stop", "status", "autostart", "send", "read",
        "onboard", "persona", "agents", "roster", "inbox", "ack", "rebind", "claim", "reply",
        "issue-handoff", "issue-rotation", "rotate-secret", "join", "policy-ack", "check-in", "ack-control", "poll", "quiet",
        "policy-set", "policy-show", "control-stop", "control-status", "session-presence",
        "rename",
        "claim-recovery",
        "control-issue",
        "codex-wake", "issue-enrollment", "migrate-channels", "migrate-profile-scope",
    }
    assert all(callable(operation) for operation in launcher.CLI_OPERATIONS.values())
    command_action = next(action for action in build_parser()._actions
                          if isinstance(action, argparse._SubParsersAction))
    assert set(command_action.choices) == set(launcher.CLI_OPERATIONS)


def test_inbox_and_read_page_limits_reject_locally_with_pagination_help(capsys):
    from src.agentbus_parser_client import parse_args

    for command in ("inbox", "read"):
        for limit in ("0", "1000", "nope"):
            with pytest.raises(SystemExit) as exit_info:
                parse_args([command, "--limit", limit])
            assert exit_info.value.code == 2
            assert "1 to 200" in capsys.readouterr().err
        assert parse_args([command, "--limit", "200"]).limit == 200


def test_quiet_preference_persists_without_changing_participation(launcher, monkeypatch, capsys):
    from src.agentbus_parser_client import parse_args

    profile = {"identity": "agentbus:one", "participation": {"stopped": False}}
    saved = []
    monkeypatch.setattr(launcher, "checked_profile", lambda _values, _identity: (profile, {}))
    monkeypatch.setattr(launcher, "save_profile", lambda _values, value: saved.append(dict(value)))
    for mode in ("on", "status", "off"):
        assert launcher.cli_quiet(parse_args(["quiet", "--identity", "agentbus:one", mode]),
                                  None, {}) == 0
    assert [item["quiet_mode"] for item in saved] == [True, False]
    assert profile["participation"]["stopped"] is False
    assert capsys.readouterr().out == "QUIET ON\nQUIET ON\nQUIET OFF\n"


def test_join_retries_with_same_session_secret_and_keeps_legacy_uuid(launcher, tmp_path, monkeypatch, capsys):
    from src import agentbus_codex_wake_client as wake

    state = tmp_path / "profiles"
    state.mkdir()
    identity = "agentbus:old"
    chat_id = "f149a35a-7467-49a7-b14e-f542e691379f"
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766"}
    profile = {"schema_version": 2, "identity": identity, "chat_id": chat_id,
               "repo": "agentbus", "display_name": "Old", "inbox_id": "inbox-1",
               "channel": "C123", "service_url": values["AGENTBUS_URL"]}
    (state / f"{identity}.json").write_text(json.dumps(profile))
    handoff = tmp_path / "handoff"
    handoff.write_text("one-use-operator-handoff-token-with-entropy")
    handoff.chmod(0o600)
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), values))
    attempts = []

    def fake_api(_values, path, payload=None, **_kwargs):
        if path == "/v1/info":
            return scope_info()
        attempts.append(payload)
        if len(attempts) == 1:
            raise launcher.ClientError("transport uncertain")
        return {"chat_id": chat_id, "session_id": "session-1", "revision": "revision-1",
                "channel": "C123", "inbox_id": "inbox-1", "high_water_cursor": 0,
                "values": {"initial_interval_seconds": 60, "backoff_factor": 2,
                           "max_interval_seconds": 1920}, "sources": {}, "ack_required": True}

    monkeypatch.setattr(launcher, "api", fake_api)
    enrolled = []
    monkeypatch.setattr(wake, "enroll_current", lambda _values, selected: enrolled.append(selected) or {"status": "bound"})
    assert launcher.main(["join", "--identity", identity, "--handoff-file", str(handoff)]) == 1
    pending = json.loads((state / f"{identity}.json").read_text())["participation_pending"]
    assert launcher.main(["join", "--identity", identity]) == 0
    committed = json.loads((state / f"{identity}.json").read_text())
    assert attempts[0]["session_secret"] == attempts[1]["session_secret"] == pending["session_secret"]
    assert committed["chat_id"] == chat_id and committed["participation"]["session_id"] == "session-1"
    assert "participation_pending" not in committed
    assert enrolled == [identity]
    capsys.readouterr()


def test_policy_ack_updates_local_budget_after_exact_revision_receipt(launcher, tmp_path, monkeypatch, capsys):
    from src import agentbus_poll_client as poll

    state = tmp_path / "profiles"
    state.mkdir()
    identity = "agentbus:flower"
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766"}
    profile = {"schema_version": 2, "identity": identity, "chat_id": "chat-1",
               "repo": "agentbus", "inbox_id": "inbox-1", "channel": "C123",
               "service_url": values["AGENTBUS_URL"],
               "participation": {"session_id": "session-1", "session_secret": "x" * 32,
                                 "policy_values": {"presentation_budget_bytes": None}}}
    path = state / f"{identity}.json"
    path.write_text(json.dumps(profile))
    updated = {"presentation_budget_bytes": 32}
    poll.change_spool(path, profile, lambda spool: poll._append(spool, {
        "kind": "POLICY_CHANGED", "id": "revision-2", "revision": "revision-2",
        "values": updated, "sources": {},
    }))
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), values))
    monkeypatch.setattr(launcher, "api", lambda _values, route, *_args, **_kwargs:
                        scope_info() if route == "/v1/info"
                        else {"receipt": "receipt-2"})
    monkeypatch.setattr(poll, "ensure_worker", lambda *_args: None)
    assert launcher.main(["policy-ack", "--identity", identity, "revision-2"]) == 0
    assert json.loads(path.read_text())["participation"]["policy_values"] == updated
    assert poll.peek_event(path, profile) is None
    assert capsys.readouterr().out == "ACK receipt-2 POLICY\n"


def test_poll_cli_uses_budgeted_default_and_full_json_on_demand(launcher, tmp_path, monkeypatch, capsys):
    from src import agentbus_poll_client as poll

    state = tmp_path / "profiles"
    state.mkdir()
    identity = "agentbus:flower"
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766"}
    profile = {"schema_version": 2, "identity": identity, "chat_id": "chat-1",
               "repo": "agentbus", "inbox_id": "inbox-1", "channel": "C123",
               "service_url": values["AGENTBUS_URL"],
               "participation": {"session_id": "session-1", "session_secret": "x" * 32,
                                 "policy_values": {"presentation_budget_bytes": 32}}}
    path = state / f"{identity}.json"
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), values))
    monkeypatch.setattr(launcher, "api", lambda *_args, **_kwargs:
                        scope_info())
    monkeypatch.setattr(poll, "ensure_worker", lambda *_args: None)

    def queue(cursor):
        poll.change_spool(path, profile, lambda spool: poll._append(spool, {
            "kind": "MESSAGE", "id": str(cursor), "message": {
                "cursor": cursor, "sender": "agentbus:peer", "recipient": "all",
                "kind": "message", "audience": "broadcast", "text": "🌼" * 100,
            },
        }))

    queue(1)
    assert launcher.main(["poll", "--identity", identity]) == 0
    assert len(capsys.readouterr().out.strip().encode("utf-8")) <= 32
    queue(2)
    assert launcher.main(["poll", "--identity", identity, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["message"]["text"] == "🌼" * 100


def test_auxiliary_control_ack_does_not_stop_client_polling(launcher, tmp_path, monkeypatch, capsys):
    from src import agentbus_poll_client as poll

    state = tmp_path / "profiles"
    state.mkdir()
    identity = "agentbus:flower"
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766"}
    profile = {"schema_version": 2, "identity": identity, "chat_id": "chat-1",
               "repo": "agentbus", "inbox_id": "inbox-1", "channel": "C123",
               "service_url": values["AGENTBUS_URL"],
               "participation": {"session_id": "session-1", "session_secret": "x" * 32}}
    path = state / f"{identity}.json"
    path.write_text(json.dumps(profile))
    poll.change_spool(path, profile, lambda spool: poll._append(spool, {
        "kind": "CONTROL", "id": "nudge-1",
        "control": {"control_id": "nudge-1", "kind": "nudge", "reason": "check in"},
    }))
    monkeypatch.setattr(launcher, "configuration", lambda **kwargs: (Path("/unused"), values))
    monkeypatch.setattr(launcher, "api", lambda _values, route, *_args, **_kwargs:
                        scope_info() if route.endswith("/self-presence")
                        else {"receipt": "nudge-receipt", "directive": "CONTINUE"})
    assert launcher.main(["ack-control", "--identity", identity, "nudge-1"]) == 0
    assert json.loads(path.read_text())["participation"].get("stopped") is None
    assert poll.peek_event(path, profile) is None
    assert capsys.readouterr().out == "ACK nudge-receipt RECORDED\n"
    report_file = tmp_path / "checkpoint.json"
    report = {"activity": "reviewing CI", "blockers": [], "waiting_on": [], "work_refs": []}
    report_file.write_text(json.dumps(report))
    payloads = []

    def checkpoint_api(_values, route, payload=None, **_kwargs):
        if route.endswith("/self-presence"):
            return scope_info()
        if route == "/v1/info":
            return scope_info()
        payloads.append(payload)
        return {"receipt": "checkpoint-receipt", "directive": "CONTINUE"}

    monkeypatch.setattr(launcher, "api", checkpoint_api)
    assert launcher.main(["ack-control", "--identity", identity, "checkpoint-1",
                          "--report-file", str(report_file)]) == 0
    assert payloads == [{"report": report}]
    assert launcher.main(["ack-control", "--identity", identity, "poll-1", "--answer", "review pending"]) == 0
    assert payloads[-1] == {"report": None, "answer": "review pending"}
    assert json.loads(path.read_text())["participation"].get("stopped") is None


def test_join_requests_slack_approval_and_recovers_without_credential_file(launcher, tmp_path, monkeypatch, capsys):
    from src import agentbus_codex_wake_client as wake
    from src import agentbus_enrollment_client as enrollment
    state = tmp_path / 'profiles'
    state.mkdir()
    identity = 'agentbus:approval'
    chat_id = 'f149a35a-7467-49a7-b14e-f542e691379f'
    values = {'AGENTBUS_CONSUMER_STATE_DIR': str(state), 'AGENTBUS_URL': 'http://127.0.0.1:8766'}
    profile = {'schema_version': 2, 'identity': identity, 'chat_id': chat_id,
               'repo': 'agentbus', 'display_name': 'Approval', 'inbox_id': None, 'channel': None,
               'service_url': values['AGENTBUS_URL'], 'ack_cursor': 0, 'observed_cursor': 0}
    path = state / f'{identity}.json'
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(launcher, 'configuration', lambda **kwargs: (Path('/unused'), values))
    attempts = []
    def fake_api(_values, route, body=None, **_kwargs):
        assert route == '/v1/sessions', 'no pre-enrollment information read'
        attempts.append(dict(body))
        if len(attempts) in {1, 3}:
            return {'status': 'approval_required', 'request_id': body['approval_request_id']}
        if len(attempts) == 2:
            raise launcher.ClientError('transport uncertain')
        return {'chat_id': chat_id, 'session_id': 'session-1', 'revision': 'revision-1',
                'channel': 'C222', 'inbox_id': 'inbox-2', 'high_water_cursor': 0,
                'values': {'initial_interval_seconds': 60, 'backoff_factor': 2, 'max_interval_seconds': 1920},
                'sources': {}, 'ack_required': True}
    monkeypatch.setattr(launcher, 'api', fake_api)
    sleeps = []
    monkeypatch.setattr(enrollment.time, 'sleep', sleeps.append)
    bindings = []
    monkeypatch.setattr(wake, 'enroll_current', lambda *_: bindings.append(True) or {'status': 'workspace_disabled'})
    assert launcher.main(['join', '--identity', identity]) == 0
    pending = json.loads(path.read_text())['participation_pending']
    output = capsys.readouterr().out
    assert '/agentbus approve ' + pending['approval_request_id'] in output
    assert pending['session_secret'] not in output
    assert 'participation' not in json.loads(path.read_text()) and bindings == []
    assert launcher.main(['join', '--identity', identity]) == 1
    assert launcher.main(['join', '--identity', identity, '--wait-for-approval']) == 0
    assert attempts[0] == attempts[1] == attempts[2] == attempts[3]
    assert sleeps == [2.0]
    assert attempts[0]['profile_chat_id'] == chat_id and 'enrollment_token' not in attempts[0]
    committed = json.loads(path.read_text())
    assert committed['channel'] == 'C222' and committed['participation']['session_secret'] == pending['session_secret']
    assert 'participation_pending' not in committed and bindings == [True]


def test_new_approval_request_preserves_old_private_stage_and_denies_service_switch(launcher, tmp_path, monkeypatch, capsys):
    state = tmp_path / 'profiles'
    state.mkdir()
    identity = 'agentbus:approval'
    request_id = 'c56fbb77-adc6-47fb-8c15-c8af21a9269b'
    values = {'AGENTBUS_CONSUMER_STATE_DIR': str(state), 'AGENTBUS_URL': 'http://127.0.0.1:8766'}
    old = {'session_secret': 'expired-secret-'.ljust(40, 'x'), 'approval_request_id': request_id,
           'service_url': values['AGENTBUS_URL']}
    profile = {'schema_version': 2, 'identity': identity, 'chat_id': 'f149a35a-7467-49a7-b14e-f542e691379f',
               'repo': 'agentbus', 'display_name': 'Approval', 'inbox_id': None, 'channel': None,
               'service_url': values['AGENTBUS_URL'], 'participation_pending': old}
    path = state / f'{identity}.json'
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(launcher, 'configuration', lambda **kwargs: (Path('/unused'), values))
    attempts = []
    def fake_api(_values, route, body=None, **_kwargs):
        attempts.append(dict(body))
        return {'status': 'approval_required', 'request_id': body['approval_request_id']}
    monkeypatch.setattr(launcher, 'api', fake_api)
    assert launcher.main(['join', '--identity', identity, '--new-enrollment-request']) == 0
    pending = json.loads(path.read_text())['participation_pending']
    archive = state / '.enrollment-recovery' / f'{identity}-{request_id}.json'
    assert json.loads(archive.read_text()) == old
    assert archive.stat().st_mode & 0o777 == 0o600
    assert pending['approval_request_id'] != request_id and pending['session_secret'] != old['session_secret']
    assert old['session_secret'] not in capsys.readouterr().out
    values['AGENTBUS_URL'] = 'http://different-service:8766'
    assert launcher.main(['join', '--identity', identity]) == 1
    assert len(attempts) == 1 and json.loads(path.read_text())['participation_pending'] == pending
