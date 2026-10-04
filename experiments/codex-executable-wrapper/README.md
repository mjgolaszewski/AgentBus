# Experimental Codex executable wrapper

**Unsupported experimental VS Code configuration. Not a dependable AgentBus
wake path or operator default.** The wrapper and adapter proxy mode are off
unless a custodian explicitly configures them. No live shared-thread pilot
has proved this arrangement.

The current VS Code Codex extension owns saved threads through the app-server it
starts. A competing AgentBus app-server cannot resume those threads. The installed
extension has a `chatgpt.cliExecutable` override explicitly marked **DEVELOPMENT
ONLY**; its own description warns that parts of the extension may break. This
prototype explores whether a custodian could use that override to connect the
extension **and AgentBus** to one separately managed, remote-control-capable
Codex app-server daemon. Code and fake-host tests prove the selected commands,
not that the extension and adapter can safely share thread ownership or finish
a real wake turn.

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

## Custodian-run isolated pilot

1. Open a separate VS Code profile with no active work and record its original
   `chatgpt.cliExecutable` value. Choose the **real** Codex executable and a
   managed app-server daemon with remote control and a private same-user Unix
   control socket. Do not use the active workspace's owning app-server.
2. Check the real CLI's `--version`, the managed daemon's reported version and
   identity, and the app-server initialization result. Confirm the exact socket
   belongs to that daemon, is accessible only to the intended Unix user, and
   is responsive. A socket file alone is insufficient.
3. Arrange for both the isolated VS Code extension host and the AgentBus wake
   worker to inherit the **same** `AGENTBUS_EXPERIMENTAL_CODEX_BINARY` and
   `AGENTBUS_EXPERIMENTAL_CODEX_CONTROL_SOCKET` values. A value exported in a
   later terminal will not reach an already-running extension host. Set the
   profile's application-scoped `chatgpt.cliExecutable` to the absolute wrapper
   path, then reload that idle profile.
4. Set `AGENTBUS_CODEX_HOST_MODE=experimental_vs_code_proxy` only for the test
   adapter worker. Run `agentbus codex-wake workspace-enable`, join or bind a
   disposable saved conversation, and inspect `agentbus codex-wake status
   --identity REPO:NAME`. The adapter uses the proxy for both enrollment and
   wake; it never falls back to a competing stdio host in this mode.
5. Send one addressed test message to that exact chat. Observe that the UI stays
   usable while AgentBus starts a turn through its second proxy and receives an
   exact terminal notification. `eligible` status or `turn/start` acceptance
   alone is not proof of delivery.

Rollback in order: run `agentbus codex-wake workspace-disable` and let active
turns drain to terminal; remove the experimental mode and two endpoint values
from the test worker and extension-host environment; restore the original
`chatgpt.cliExecutable` value and reload the idle VS Code profile; then stop
the test daemon after all turns are terminal. Verify that wake status has
returned to notification-only. Do not stop or restart an active shared owner
to perform this experiment.

Filesystem ownership under one Unix UID does not authenticate the daemon
against another same-user process. A successful pilot still needs an explicit
decision to accept the development-only override, or a supported owner
endpoint, before this can be a dependable product path.
