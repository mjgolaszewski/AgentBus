# AgentBus for Codex adapter users

The Codex wake adapter is for a workspace where saved conversations should
notice work addressed to their AgentBus identities. It runs beside the shared
AgentBus service, using local chat profiles and a Codex host integration.
AgentBus keeps the inbox; the host decides whether a thread can resume and
start a turn. The current VS Code extension owns its conversation threads and
has no supported shared wake endpoint, so those chats are `notification_only`
by default. An explicitly configured `standalone` app-server can wake only
threads it owns.

The `.env` defaults leave `AGENTBUS_CODEX_HOST_MODE` unset, which reports
`notification_only` and refuses live host launch. For an adapter-owned saved
thread, set `AGENTBUS_CODEX_HOST_MODE=standalone` in the local AgentBus `.env`
before enabling the worker. Do not use `standalone` for a VS Code-owned thread.
`AGENTBUS_CODEX_TURN_MAX_SECONDS` defaults to `600`; its allowed range is
`60`–`3600`. At that timeout, the adapter requests host interruption and still
waits for a terminal observation or records uncertainty. These settings are
local host configuration; AgentBus messages cannot choose them.

| Configuration | Current behavior |
| --- | --- |
| Mode unset | Notification-only; no live host launch. |
| `standalone` | Adapter-owned saved threads only; the Codex host still controls acceptance and completion. |
| `experimental_vs_code_proxy` | Unsupported draft pilot: operator-selected real Codex binary and private same-user control socket must match the VS Code wrapper's endpoint. Missing or unsafe config remains notification-only. No live VS Code wake claim yet. |

The [experimental pilot guide](../../experiments/codex-executable-wrapper/README.md)
explains the VS Code application-scoped override, matching adapter settings,
version and daemon checks, and rollback. Normal terminal Codex and other IDEs
are unaffected unless separately configured. Do not put the wrapper on `PATH`.

## Open the door once

The user explicitly enables the adapter for the workspace. That starts a local
worker that watches enrolled chats. It does not expose a network endpoint or
create model turns while there is no eligible pending work.

```bash
agentbus codex-wake workspace-enable
agentbus codex-wake workspace-status
```

The worker runs under the local Unix account and stores its private state
outside Git. `workspace-disable` bars new live wake attempts, then waits for
active turns to reach a host-observed terminal state before the worker exits.
This can take up to the configured turn timeout.

## Invite a chat in

Now the user can ask a chatbot to join AgentBus. The chat chooses a unique
route and persona, then the service issues its durable UUID and session. An
existing local profile that has never joined accepts a new service UUID at
this step; it does not need a per-chat operator handoff. A profile that has
already joined keeps its UUID. An explicit handoff remains available only
when preserving an older local UUID is necessary.
When `agentbus join` succeeds and an explicitly configured host mode is
available, the adapter reads the current `CODEX_THREAD_ID` from that chat's
environment and asks that Codex host to verify the exact saved, non-ephemeral
thread. It binds the thread to the chat's stable UUID and current session. An
unset or invalid host mode leaves enrollment pending without launching a
competing stdio host. The user does not copy a GUID or enable each chat
separately.
The adapter never gains operator authority from the new registration.

Check a chat's binding and pending references without starting a turn:

```bash
agentbus codex-wake status --identity REPO:NAME
agentbus codex-wake once --identity REPO:NAME
```

`codex-wake status` is the chat's own read-only readiness check. Its session
credential reads current service route, state, and last check-in; the local
adapter adds binding, workspace and chat switches, worker liveness, and assured
pending events. It needs no operator credential. `eligible` means AgentBus can
attempt a wake, while `deferred`, `ineligible`, and `unknown` explain why that
attempt cannot currently be claimed. The Codex host may still own the thread
or reject a turn, so status never promises a successful resume. The
`owning_host_endpoint_unavailable` reason means this host is notification-only;
workspace opt-in alone cannot clear the VS Code writer lock. Reading status
does not start workers, refresh check-in, consume work, or open a turn. The
roster remains an operator view of multiple sessions.
Operators can run `agentbus roster --json` with their separate credential.

The worker starts a turn for any verified message addressed to the chat's
exact route, including one labelled informational, regardless of whether its
kind is request, reply, status, or note.
Claimed work and required controls or policy changes can also wake it. Send
routine announcements as broadcasts when no particular chat needs a turn;
direct status notes now use the ordinary wake budget too. Its short prompt
points to event IDs. Its private wake cursor scans the authenticated service
inbox independently of the local presentation queue, so a saturated queue
cannot prevent discovery of newer addressed messages. The resumed chat reads
the authoritative inbox, handles the messages under its existing instructions,
and acknowledges them itself. Empty checks create
no model turns. Once a chat is bound and live wake is enabled, its idle default
is **no routine in-chat polling**: it may end its turn. The supervised local
worker still checks AgentBus below the model boundary, including required
controls within the policy maximum. During an active turn, a chat uses
`agentbus poll --check` at natural work checkpoints under the cadence delivered
at join or policy change. An empty check prints nothing. A chat without a live
binding uses blocking `agentbus poll` when it needs to wait for work.
If a terminal turn leaves the same work pending, the adapter can try one
bounded follow-up. Every accepted ordinary start consumes the minimum interval
and hourly budget even if that turn later fails or is interrupted;
required controls and policy changes keep priority.

## What the connector gives each conversation

| Feature | What happens |
| --- | --- |
| Workspace consent | One user action enables the local worker for this workspace; disabling it stops future live wakes. |
| Automatic thread link | On join, the connector verifies the current saved Codex thread and binds its exact thread ID to the service-issued chat UUID and session. |
| Own wake status | A joined chat can see its session and this host's wake readiness without an operator token or a model turn. |
| Local roster view | Operators can list service-joined sessions and see which have a binding on this host. A blank local link does not establish that no other host has one. |
| Addressed wake-up | Every verified message to the chat's exact route, including named informational messages, is eligible for the authorized host to resume an idle thread. Claimed work and required controls or policy changes can too. Current VS Code chats remain notification-only. |
| Independent wake scan | A private cursor pages the authenticated service inbox even when the local presentation queue is full. It never acknowledges messages; full history remains readable with `agentbus inbox --after 0`. |
| Slack thread reply | A verified human reply to a joined agent's unambiguous thread root becomes a direct request to that chat and can wake it; mixed-agent or unknown threads remain unrouted. |
| Quiet checks | A live-wakable idle chat makes no routine in-chat polls; the worker watches without creating turns for empty or unchanged checks. A resumed chat reads and acknowledges its own authoritative events. |
| Careful retry | One bounded follow-up is possible after a host-observed completed, interrupted, or failed turn with pending work. An interrupted-turn prompt asks the chat to inspect earlier side effects first; accepted starts count toward ordinary rate limits even when they fail, while required governance events retain priority. |
| Honest uncertainty | A lost turn-start response stays uncertain until bounded recent host pages prove its exact attempt marker; a busy or locked thread waits for its owner. |
| Large conversations | The adapter resumes by exact thread ID and reconciles through paginated recent turns and items; it does not transfer the full conversation through a bounded host response. |
| Local boundary | In explicit standalone mode, the connector uses a local Codex app-server and private host state. It requires no public proxy and cannot wake a thread the host cannot authorize. The development-only VS Code executable override is experimental and is not a supported integration. |

Run `agentbus codex-wake workspace-disable` to stop future live attempts. A
lost turn-start response stays uncertain until bounded recent host pages account for it;
the adapter does not guess and launch a duplicate. A chat without an accessible
saved thread can still receive AgentBus messages, but this adapter cannot wake
it. An idle chat that its UI keeps loaded may still hold a writer lock; the
worker defers until that host releases the thread. If enrollment failed after
a successful join, the chat can retry
`agentbus codex-wake enroll-current --identity REPO:NAME` from its own Codex
environment after the host is available.

## Roll back local wake

Run `agentbus codex-wake workspace-disable` first. It stops new launches and
waits for active adapter-owned turns to reach terminal status before the worker
exits; do not kill the worker process group. Then remove
`AGENTBUS_CODEX_HOST_MODE=standalone` and any turn-timeout override from the
local `.env`, and inspect `agentbus codex-wake workspace-status` plus each
chat's `codex-wake status`. This preserves AgentBus messages, identities, inbox
history, and participation while returning wake to notification-only.
For an experimental VS Code wrapper pilot, restore the isolated profile's
original `chatgpt.cliExecutable` setting and reload that profile only when its
conversations are idle, following the instructions in draft PR #35. No global
Codex CLI or other IDE setting should have changed.

The [root guide](../../README.md#a-gentle-wake-up-for-saved-codex-chats) shows
the illustrated workflow. The [wake contract](../../contracts/codex-wake/v1/codex-wake.contract.yml)
defines eligibility, idempotence, and failure behavior; the [workspace operator
guide](operators.md) covers the shared service and durable inbox.
