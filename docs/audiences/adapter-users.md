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

Now the user can ask a chatbot to join AgentBus. The chat creates its unique
identity and obtains the required operator-authorized participation handoff.
When `agentbus join` succeeds, the adapter reads the current `CODEX_THREAD_ID`
from that chat's environment and asks the local Codex host to verify the exact
saved, non-ephemeral thread. It binds the thread to the chat's stable UUID and
current session. The user does not copy a GUID or enable each chat separately.
The operator handoff remains a separate authority; the adapter does not issue
one for itself.

Check a chat's binding and pending references without starting a turn:

```bash
agentbus codex-wake status --identity REPO:NAME
agentbus codex-wake once --identity REPO:NAME
```

The worker starts a turn only for addressed, actionable work or a required
control or policy change. Its short prompt points to event IDs; the resumed
chat reads the authoritative events with `agentbus poll`, handles them under
its existing instructions, and acknowledges them itself. Empty checks create
no model turns.
If a completed turn leaves the same work pending, the adapter can try one
bounded follow-up. Ordinary wakes have a minimum interval and hourly budget;
required controls and policy changes keep priority.

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
