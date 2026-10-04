# Experimental Codex executable wrapper

**Unsupported prototype. Not an AgentBus wake transport, release dependency, or
operator recommendation.** Nothing here is installed or enabled by AgentBus.

The current VS Code Codex extension owns saved threads through the app-server it
starts. A competing AgentBus app-server cannot resume those threads. The installed
extension has a `chatgpt.cliExecutable` override explicitly marked **DEVELOPMENT
ONLY**; its own description warns that parts of the extension may break. This
prototype explores whether a custodian could use that override to connect the
extension to a separately managed, remote-control-capable Codex app-server daemon.
It has not proven that the daemon and extension can safely share thread ownership,
that AgentBus can reach the same host, or that a live conversation can finish a
wake turn through this arrangement.

**Scope:** Only the VS Code Codex extension configured with
`chatgpt.cliExecutable` would invoke this wrapper. The setting is application
scoped, so a pilot needs an isolated VS Code profile rather than assuming a single
workspace window is isolated. Ordinary `codex` terminal commands and other IDEs
continue using their own executables unless separately configured. Do not put this
wrapper on `PATH`, replace the real Codex binary, or install it globally.

## Mechanism

The observed extension version `26.930.31730` launches:

```text
codex -c features.code_mode_host=true app-server --analytics-default-enabled
```

The wrapper recognizes this invocation and executes the configured real Codex
binary as:

```text
codex -c features.code_mode_host=true app-server proxy --sock PRIVATE_SOCKET
```

The proxy bridges stdio to a pre-existing app-server control socket. The wrapper
does **not** bootstrap or start a daemon. It refuses to start an independent host
when the socket is absent, non-socket, owned by a different Unix user, or accessible
to group/other users. Ordinary CLI invocations pass through to the real binary.
Unknown app-server launch flags fail closed. `--analytics-default-enabled` cannot
be forwarded to `proxy`; the daemon's analytics policy must be set independently.
Likewise, forwarding `-c features.code_mode_host=true` to the proxy has not been
shown to configure the underlying daemon. This is a semantic compatibility gap,
not an assertion of equivalence.

The wrapper reads only two process-environment values set by the host custodian:

| Variable | Meaning |
| --- | --- |
| `AGENTBUS_EXPERIMENTAL_CODEX_BINARY` | Absolute path to the real Codex executable; cannot point back to the wrapper. |
| `AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET` | Absolute path to an already-live, private same-user Unix control socket. |
| `AGENTBUS_CODEX_HOST_MODE=experimental_vs_code_proxy` | Explicit adapter-side opt-in; unset remains notification-only. The extension wrapper does not read this setting. |

No AgentBus message or agent route can select either endpoint. A shared Unix account
still weakens this boundary: another same-user process can potentially influence
environment, socket, and Codex state. The socket check is a guard against accidental
misrouting, not a security claim against a hostile same-user process.

## Test without touching VS Code

```sh
python -m pytest -q tests/test_codex_wrapper_experiment.py tests/test_codex_host_client.py
```

The tests launch a fake executable and temporary Unix socket. They prove exact
proxy command selection, a JSON-RPC round trip through an inert fake, ordinary
CLI delegation, and fail-closed behavior for absent or unsafe sockets and
unrecognized app-server flags. They do **not** exercise the Codex daemon, VS
Code extension, real thread locking, terminal notifications, or user permissions.
No live configuration or shared service should change on this evidence.

## Conditions for a custodian-run pilot

The custodian would need an isolated test profile, a managed daemon with its private
control socket, a known matching Codex binary, an extension restart using the
development-only override, and a rollback to the original extension executable.
The extension host must inherit both wrapper environment values; values set in a
later terminal do not retroactively reach an already-running extension host. The
`chatgpt.cliExecutable` value must be the absolute wrapper path visible to that
host, while `AGENTBUS_EXPERIMENTAL_CODEX_BINARY` points to the real executable.
The experimental AgentBus adapter mode must separately be given the **same** real
binary and socket through operator-controlled environment configuration; a wrapper
on the VS Code side alone still leaves AgentBus on a competing app-server.
Before switching the override, the custodian can use the fake-host tests and verify
that the private control socket actually exists. The wrapper never creates it. A
socket file alone does not prove that the daemon is responsive or compatible.
The pilot should record the real CLI's `--version`, the daemon's reported version
and identity, and the app-server initialization result, then confirm they refer to
the expected managed owner. Filesystem ownership under one Unix UID does not
authenticate the daemon against another same-user process. Restore the original
`chatgpt.cliExecutable` value and extension-host environment if the pilot fails;
only stop the test daemon after active turns have reached terminal state.
The pilot must show one saved conversation remains usable in the UI while a second
authorized proxy can resume its exact thread, start a turn, observe a terminal
notification, and leave the UI healthy. A failed pilot must leave AgentBus in
notification-only mode. A successful pilot still needs a supported owner endpoint or
an explicit acceptance of the development-only override before this can be a
dependable product path.
