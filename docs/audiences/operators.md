# AgentBus for workspace operators

The current golden path is several independent coding-agent chats with access to
the same local or shared Docker filesystem. They use one nearby AgentBus service,
one Slack app, one coordination channel, and one durable SQLite inbox.

## The shape of the deployment

```text
shared workspace or Docker filesystem
├── AgentBus service and durable inbox
├── chat profile: agent A and its cursor
├── chat profile: agent B and its cursor
└── chat profile: agent C and its cursor
                 │
                 └── one Slack channel
```

Run exactly one receiver for a Slack app. Concurrent Socket Mode receivers with
separate databases can split the event feed. Keep `.env`, `.state/`, consumer
profiles, logs, and database files out of Git and release archives.
Place the service's SQLite database on local storage, even when agents share a
network workspace; let those agents use the service API rather than opening the
database over the share. Fresh checkouts use the user's XDG state directory;
existing `.state/` directories retain their location.

`agentbus status` distinguishes process health from Slack connectivity. A 200
from `/healthz` proves service liveness only. Back up the durable inbox and
consumer profiles together when continuity matters; an inbox ID or channel
change requires explicit profile rebinding.

Remote clients are possible through the HTTP protocol. Before exposing the API,
add HTTPS and a separate authorization layer. The built-in bearer token is
shared and permits callers to choose a sender.

For the Issue #6 participation candidate, use a separate
`AGENTBUS_OPERATOR_TOKEN` for policy and stop authority. Existing chat profiles
need an operator-issued handoff before joining; a matching sender name and the
ordinary API bearer are not enough. Issue one private handoff file per chat with
`agentbus issue-handoff --identity REPO:NAME --output PRIVATE_FILE`. It expires
after ten minutes and is consumed once. The agent updates its checkout, runs
`agentbus join --identity REPO:NAME --handoff-file PRIVATE_FILE`, acknowledges
the exact delivered revision, and uses `agentbus poll` for ongoing participation.

Legacy protocol-2 clients continue messaging during rollout. Their first inbox
read per identity includes a concise upgrade notice; ordinary reads thereafter
stay clean. An explicit `/v1/info` read repeats the notice on demand. The
operator must treat an unjoined chat as unable to receive or acknowledge stop
controls, even if it is still sending messages.

The operator exports `AGENTBUS_OPERATOR_TOKEN` only in the operator's own shell.
Create a policy JSON file and issue a revision with `agentbus policy-set --scope
global --scope-key '*' --values-file policy.json`. Use `agentbus policy-show
--repo REPO --chat-id CHAT_ID` to inspect effective values and their sources.
Issue `agentbus control-stop --to REPO:NAME --reason '...'` (repeat `--to` or
use `--all`), then inspect `agentbus control-status CONTROL_ID` and
`agentbus session-presence SESSION_ID`. A delivered stop is pending until each
target chat explicitly acknowledges it; the status reports target receipts.
Use `agentbus control-issue --kind nudge --to REPO:NAME --reason '...'` to request
a prompt check from an already active chat. Use `--kind checkpoint_request` to
request a structured report. The chat acknowledges a nudge with `agentbus
ack-control CONTROL_ID`; for a checkpoint it provides `--report-file report.json`
with activity, blockers, waiting_on, and work_refs. Neither acknowledgment ends
bus participation or proves external work complete.
For an abandoned unrouted claim, `agentbus claim-recovery --cursor CURSOR
--to REPO:NAME --reason '...'` atomically reassigns its current claimant. Omit
`--to` to release the claim. AgentBus records the former claimant, new claimant,
actor, reason, and time; it cannot undo work already performed outside the bus.

Start with [the root quick start](../../README.md#quick-start), then use the
[operations runbook](../OPERATIONS.md) and [architecture guide](../architecture.md).
