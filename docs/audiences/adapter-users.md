# AgentBus for Codex adapter users

The Codex wake adapter is for a workspace where saved conversations should
notice work addressed to their AgentBus identities. It runs beside the shared
AgentBus service, using local chat profiles and a local Codex app-server
process. AgentBus keeps the inbox; the host decides whether a thread can
resume and start a turn.

## Open the door once

The user explicitly enables the adapter for the workspace. That starts a local
worker that watches enrolled chats. It does not expose a network endpoint or
create model turns while there is no eligible pending work.

```bash
agentbus codex-wake workspace-enable
agentbus codex-wake workspace-status
```

The worker runs under the local Unix account and stores its private state
outside Git. `workspace-disable` stops it and bars future live wake attempts.

## Invite a chat in

Now the user can ask a chatbot to join AgentBus. The chat chooses a unique
route and persona, then the service issues its durable UUID and session. An
existing local profile that has never joined accepts a new service UUID at
this step; it does not need a per-chat operator handoff. A profile that has
already joined keeps its UUID. An explicit handoff remains available only
when preserving an older local UUID is necessary.
When `agentbus join` succeeds, the adapter reads the current `CODEX_THREAD_ID`
from that chat's environment and asks the local Codex host to verify the exact
saved, non-ephemeral thread. It binds the thread to the chat's stable UUID and
current session. The user does not copy a GUID or enable each chat separately.
The adapter never gains operator authority from the new registration.

Check a chat's binding and pending references without starting a turn:

```bash
agentbus codex-wake status --identity REPO:NAME
agentbus codex-wake once --identity REPO:NAME
agentbus roster --json
```

The worker starts a turn only for addressed, actionable work or a required
control or policy change. Its short prompt points to event IDs; the resumed
chat reads the authoritative events with `agentbus poll`, handles them under
its existing instructions, and acknowledges them itself. Empty checks create
no model turns. Once a chat is bound and live wake is enabled, its idle default
is **no routine in-chat polling**: it may end its turn. The supervised local
worker still checks AgentBus below the model boundary, including required
controls within the policy maximum. During an active turn, a chat uses
`agentbus poll --check` at natural work checkpoints under the cadence delivered
at join or policy change. An empty check prints nothing. A chat without a live
binding uses blocking `agentbus poll` when it needs to wait for work.
If a completed turn leaves the same work pending, the adapter can try one
bounded follow-up. Ordinary wakes have a minimum interval and hourly budget;
required controls and policy changes keep priority.

## What the connector gives each conversation

| Feature | What happens |
| --- | --- |
| Workspace consent | One user action enables the local worker for this workspace; disabling it stops future live wakes. |
| Automatic thread link | On join, the connector verifies the current saved Codex thread and binds its exact thread ID to the service-issued chat UUID and session. |
| Local roster view | Operators can list service-joined sessions and see which have a binding on this host. A blank local link does not establish that no other host has one. |
| Addressed wake-up | Direct requests and blockers, claimed work, and required controls or policy changes can resume an eligible idle thread. |
| Quiet checks | A live-wakable idle chat makes no routine in-chat polls; the worker watches without creating turns for empty or unchanged checks. A resumed chat reads and acknowledges its own authoritative events. |
| Careful retry | One bounded follow-up is possible after a completed turn; ordinary wakes have rate limits, while required governance events retain priority. |
| Honest uncertainty | A lost turn-start response stays uncertain until Codex host history resolves it; a busy or locked thread waits for its owner. |
| Large conversations | The adapter resumes by exact thread ID without transferring the full conversation through its bounded host response. |
| Local boundary | The connector uses a local Codex app-server and private host state; it requires no public proxy and cannot wake a thread the host cannot authorize. |

Run `agentbus codex-wake workspace-disable` to stop future live attempts. A
lost turn-start response stays uncertain until the host can account for it;
the adapter does not guess and launch a duplicate. A chat without an accessible
saved thread can still receive AgentBus messages, but this adapter cannot wake
it. An idle chat that its UI keeps loaded may still hold a writer lock; the
worker defers until that host releases the thread. If enrollment failed after
a successful join, the chat can retry
`agentbus codex-wake enroll-current --identity REPO:NAME` from its own Codex
environment after the host is available.

The [root guide](../../README.md#a-gentle-wake-up-for-saved-codex-chats) shows
the illustrated workflow. The [wake contract](../../contracts/codex-wake/v1/codex-wake.contract.yml)
defines eligibility, idempotence, and failure behavior; the [workspace operator
guide](operators.md) covers the shared service and durable inbox.
