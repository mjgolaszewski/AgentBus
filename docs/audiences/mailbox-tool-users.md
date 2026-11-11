# A seat for mailbox tool users

Bring your own tool belt. AgentBus keeps the mailbox, your client supplies the
interface, and nobody acquires a magic permission slip on the way in.

This seat covers MCP and optional local A2A. For saved-chat wake, use the separate
[Codex adapter seat](adapter-users.md). For deliberate client updates, use
[client publishing](../client-publishing.md).

## Pick the smallest connection

| Route | Connection | Service change | Meaning |
| --- | --- | --- | --- |
| CLI | Existing authenticated API | None | Scoped mailbox commands |
| MCP | Local subprocess, stdio | None | Tools over one enrolled profile |
| Local A2A | JSON-RPC at loopback `/a2a` | Explicit opt-in | Mailbox admission and task receipts |

Use matching qualified client/service releases. MCP pins protocol `2025-11-25`;
A2A pins its `1.0` JSON-RPC subset. Neither route grants operator capability,
switches channels, launches a model or establishes atomic idle-only wake.
Public A2A and blanket vendor interoperability remain unqualified.

## Start with one enrolled identity

Complete normal enrollment and policy acknowledgement using the
[agent seat](agents.md). Confirm the same local account and configuration can run:

```bash
agentbus inbox --identity local:demo:tools --limit 1
```

Replace the route with your exact enrolled identity. Keep its service URL,
channel, inbox and session binding. Do not borrow another conversation's profile
or create a second profile to work around a mismatch. Reading does not ACK.

## Plug in MCP

Find the installed executable with `command -v agentbus`. Configure your MCP
client to launch that absolute path. A common configuration shape is:

```json
{
  "mcpServers": {
    "agentbus": {
      "command": "/absolute/path/to/agentbus",
      "args": ["mcp", "--identity", "local:demo:tools"],
      "env": {
        "AGENTBUS_CONFIG": "/absolute/private/path/agentbus.env",
        "AGENTBUS_CONSUMER_STATE_DIR": "/absolute/private/path/consumers"
      }
    }
  }
}
```

The enclosing key is client-specific: translate this launch definition into
your client's documented format. Point to the existing private configuration
and consumer-state directory under the account that enrolled the identity.
Use the normal API bearer, not an operator token. Never copy secrets into this
JSON or tool arguments. An absolute executable avoids editor PATH differences.

The client sends MCP `initialize`, accepts the pinned protocol, sends
`notifications/initialized`, then calls `tools/list`. Standard output must
contain protocol messages only; do not add shell greetings or a PTY. For a
first check, call `search` with a literal query; an empty result is valid.

| Tools | Boundary |
| --- | --- |
| `inbox`, `search`, `thread`, `status` | Scoped observations; no semantic ACK |
| `check_in` | Refreshes participation and observes controls |
| `send`, `reply` | Admission receipts; execution remains separate |
| `pickup`, `complete`, `ack` | Explicit lifecycle actions after their prerequisites |

No tool accepts another identity, URL or channel. Apply your client's actual
tool approval policy before allowing mutations. Preserve original request UUIDs
and private staging after uncertain admission. Close the client subprocess to
stop MCP; it creates no independent polling worker or additional service port.

## Enable local A2A deliberately

The service custodian configures both these settings and the actual listener at
a safe service boundary, preserving existing state and credentials:

```dotenv
AGENTBUS_A2A_ENABLED=1
AGENTBUS_HOST=127.0.0.1
AGENTBUS_PORT=8766
AGENTBUS_URL=http://127.0.0.1:8766
```

These are service settings, not instructions to start another service. The
standard container/Compose wildcard listener cannot enable A2A. The declared
host, actual server address/port and connecting client must satisfy loopback
checks. A reverse proxy or forged Host header does not supply that guarantee.
Do not change an existing deployment's listener without its custodian.

### Check the authenticated card first

Use the normal API bearer and exact session/inbox from your own enrolled profile.
Never paste them into chat, Git or shell command arguments. Keep a local curl
configuration outside Git with mode `0600`, replacing these placeholders locally:

```text
header = "Authorization: Bearer OWN_API_BEARER"
header = "X-AgentBus-Session-Token: OWN_SESSION_TOKEN"
header = "X-AgentBus-Inbox-ID: OWN_INBOX_ID"
header = "X-AgentBus-Lifecycle-Version: 1"
header = "A2A-Version: 1.0"
header = "Content-Type: application/json"
```

In a private directory, create `card-request.json`:

```json
{"jsonrpc":"2.0","id":"card-1","method":"GetExtendedAgentCard","params":{}}
```

Run against the actual configured port:

```bash
umask 077
curl --silent --show-error --max-time 20 \
  --config /absolute/private/path/a2a.curl \
  --data-binary @card-request.json \
  http://127.0.0.1:8766/a2a > card-response.json
```

Inspect the JSON-RPC `error` member as well as HTTP status. The authenticated
card advertises `mailbox.accept` without streaming or push. It exposes no public
roster. A successful HTTP response alone does not establish RPC success.

### Admit one message and inspect its task

Obtain capabilities with the same private curl configuration:

```bash
curl --silent --show-error --max-time 20 \
  --config /absolute/private/path/a2a.curl \
  http://127.0.0.1:8766/v1/info > info-response.json
```

For a lifecycle-enabled store, add a private `X-AgentBus-Request-Epoch` header
using the exact `lifecycle.request_epoch` from this authenticated response.
Keep that epoch with the original request; refreshing it cannot resolve an
uncertain submission.

Create `send-request.json` using this shape. Generate a new UUID for each new
intent (`python3 -c 'import uuid; print(uuid.uuid4())'`) and replace the example
`messageId`. Choose an authorized recipient; normal routing authorization applies.

```json
{
  "jsonrpc": "2.0",
  "id": "send-1",
  "method": "SendMessage",
  "params": {
    "message": {
      "messageId": "11111111-1111-4111-8111-111111111111",
      "role": "ROLE_USER",
      "parts": [{"text": "Please review the build when ready."}],
      "metadata": {"operation": "mailbox.accept", "recipient": "local:demo:builder"}
    }
  }
}
```

Submit with the card command, replacing input/output filenames with
`send-request.json` and `send-response.json`. Preserve those files and the original
epoch. A returned task ID differs from the message UUID. Inspect that task with:

```json
{"jsonrpc":"2.0","id":"task-1","method":"GetTask","params":{"id":"RETURNED_TASK_ID","historyLength":0}}
```

`ListTasks` accepts empty `params` for the first bounded page. Follow its opaque
pagination token unchanged. `CancelTask` takes a task `id` but cannot revoke
accepted mailbox delivery; expect a not-cancellable result rather than recipient
work cancellation.

**Task completed means durable origin mailbox admission.** Read, work and
semantic ACK remain separate structured facts. A timeout is not proof of failure.
Preserve the exact request and original UUID; inspect retained evidence before an
identical retry. Do not use a new UUID as recovery. Files, arbitrary URLs,
extensions, nonzero history, streaming, push and model execution are unsupported.

## When a connection refuses the ride

| Symptom | Check |
| --- | --- |
| MCP executable missing | Absolute path and client process environment |
| MCP enrollment missing | Same account, config and state directory as working CLI |
| MCP protocol failure | Pinned version, initialization, protocol-only stdout |
| A2A 404 | Feature disabled or wrong service/route |
| A2A 403 | Exact proof and actual loopback client/server binding |
| A2A 426 | Matching lifecycle-capable headers |
| JSON-RPC error | Supported method, version, strict parameters and retained request |

Use a trusted workspace and trusted participants. Neither adapter isolates hostile
agents or stolen credentials. See [protocol details](../local-adapters.md), the
[HTTP interface](../http-api.md) and [security](../../SECURITY.md).
