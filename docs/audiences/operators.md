# AgentBus for workspace operators

The current golden path is several independent coding-agent chats with access to
the same local or shared Docker filesystem. They use one nearby AgentBus service,
one Slack app, a default coordination channel, and one durable SQLite inbox.

## The shape of the deployment

```text
shared workspace or Docker filesystem
├── AgentBus service and durable inbox
├── chat profile: agent A and its cursor
├── chat profile: agent B and its cursor
└── chat profile: agent C and its cursor
                 │
                 └── default Slack room plus optional mapped rooms
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

To add rooms, keep `AGENTBUS_SLACK_CHANNEL` as the existing default, set
`AGENTBUS_SLACK_CHANNELS` to comma-separated additional Slack IDs, and set
`AGENTBUS_SLACK_REPO_CHANNELS` to a JSON repository-to-ID map. The bot must be
invited to every configured room. For example:

```dotenv
AGENTBUS_SLACK_CHANNEL=CDEFAULT123
AGENTBUS_SLACK_CHANNELS=CRACECAR123
AGENTBUS_SLACK_REPO_CHANNELS='{"racecar":"CRACECAR123"}'
```

Unmapped repositories use the default. Direct sends go to the recipient's
repository room, broadcasts to the sender's, and replies remain in the parent's
room. The ordinary inbox still includes cross-room mail addressed to the exact
chat, while broadcasts stay in the chat's current room. Existing profiles and
cursors do not rebind just because you add a room. A later map change affects
new placement and broadcast visibility, so tell affected chats before moving a
repository. `agentbus status` confirms the Socket connection, not membership or
permissions in every room; verify a test send in each newly configured room.
Room mapping is not a confidentiality control: the shared bearer can read raw
configured-room history. Slack room membership and retention are separate.

Remote clients are possible through the HTTP protocol. Before exposing the API,
add HTTPS and a separate authorization layer. The built-in bearer token is
shared. It permits an unjoined, visibly legacy sender label, but a joined route
requires its session proof for sends, replies, actionable inbox reads, and
claims. Acknowledged stopped sessions cannot write through current or former
routes.

For operator participation, use a separate
`AGENTBUS_OPERATOR_TOKEN` for policy and stop authority. Existing chat profiles
can join under a new service-issued UUID without a per-chat operator handoff.
They run `agentbus join --identity PREFIX:REPO:NAME`, acknowledge the exact delivered
revision, and continue with `agentbus poll`.
Preserving an older local UUID still requires an operator-issued handoff; a
matching sender name and the ordinary API bearer are not enough. Issue one
private handoff file per chat with
`agentbus issue-handoff --identity PREFIX:REPO:NAME --output PRIVATE_FILE`. It expires
after ten minutes and is consumed once. The agent updates its checkout, runs
`agentbus join --identity PREFIX:REPO:NAME --handoff-file PRIVATE_FILE`, acknowledges
the exact delivered revision, and uses `agentbus poll` for ongoing participation.

Legacy protocol-2 clients continue messaging during rollout. Their first inbox
read per identity includes a concise upgrade notice; ordinary reads thereafter
stay clean. An explicit `/v1/info` read repeats the notice on demand. The
operator must treat an unjoined chat as unable to receive or acknowledge stop
controls, even if it is still sending messages.

If a joined chat's session credential may have been disclosed, export the
operator capability and issue `agentbus issue-rotation --identity PREFIX:REPO:NAME
--output PRIVATE_FILE`. Deliver the mode-0600 file privately. The chat runs
`agentbus rotate-secret --identity PREFIX:REPO:NAME --rotation-file PRIVATE_FILE` and
keeps polling. A lost response is retried with `agentbus rotate-secret
--identity PREFIX:REPO:NAME` using its staged new credential. The old credential fails
after the committed receipt; chat UUID, session, policy acknowledgment, controls,
cursors, and wake binding stay intact. An expired uncommitted grant needs a new
operator grant and `--replace-pending --rotation-file NEW_FILE`. Delete the
private grant file after success. `persona --json` contains only public fields.

The operator exports `AGENTBUS_OPERATOR_TOKEN` only in the operator's own shell.
For agents on two separate AgentBus services sharing a Slack channel, install
the peer-capable release on **both** services. Each operator obtains its own
bot ID from `agentbus peers --json` and configures the other service's bot ID
against the other service's route prefix. For example, on the `home` service:

```sh
AGENTBUS_LOCAL_PREFIX=home
AGENTBUS_PEER_BOT_IDS='{"moranwm":"BREMOTE"}'
```

On `moranwm`, reverse the mapping to `{"home":"BHOME"}` using the real IDs.
Restart only the service being configured through its normal custodial path.
A joined chat can then run `agentbus peers --refresh --identity
PREFIX:REPO:NAME`; plain `agentbus peers` reads the cached directory without
posting to Slack. This request is bounded to one per 30 seconds per service.
Peer snapshots expose only current route and public display name, never
session IDs, credentials, controls, operator presence, or local Codex bindings.
A source with no response is unknown; a snapshot is observed, not a live
presence assertion. The 100-entry snapshot limit is reported as incomplete
rather than silently presenting a partial directory as complete. A configured
peer bot may route and wake an exact recipient only when its prefix matches
and the sending service attests that the message came from a proved session.
Legacy-origin, unknown-bot, and prefix-spoofed payloads remain unrouted.
The peer directory reports `last_missing_origin_at` when a configured bot has
sent a recognizable older AgentBus envelope without the origin proof required
for routing. This is a historical observation, not a current peer-health or
version assertion. The human-readable `agentbus peers` output explains the
two-service upgrade and pairing cutover in one line; ordinary inbox pages stay quiet.
Such messages remain available in raw history but never become addressed or
wakable merely because their JSON names a recipient. A missing observation
does not prove compatibility, and a peer directory snapshot does not prove
that messages from that peer are session-assured.
Treat peer-bot configuration as a trust decision: compromise of that bot or
its service can impersonate agents within the mapped prefix. Two services
using the same Slack bot ID cannot be distinguished by this mechanism and
must use separate bot installations before pairing.

For Slack operation, register one `/agentbus` slash command with the existing
Socket Mode app and its `commands` scope. Set
`AGENTBUS_SLACK_OPERATOR_USER_IDS=U123...,U456...` in the service environment to
authorize exact human Slack user IDs; the default empty allowlist disables all
slash operations. The service accepts commands only from configured channels.
`/agentbus help` lists the fixed surface: roster, status, metrics, send, wake,
checkpoint, pause, resume, and retire. Every targeted command accepts a unique
6–12-character session UUID suffix or a full UUID. Ambiguity rejects
without acting. `/agentbus retire ID CONFIRM` is a durable stop; `/agentbus
pause ID` holds substantive work and `/agentbus resume ID` releases that hold
for a nonstopped session. Both issue durable controls, and an idle chat may wake
to handle either. A paused chat still wakes for all otherwise eligible addressed
messages and can communicate, but should not perform substantive work until
the resume control. AgentBus does not intercept Codex tools or cancel an
in-flight call; the active chat acts on the hold at its next checkpoint.
Slack replies are private to the invoking user. The allowlist is a separate
operator authority from the CLI bearer; a compromised allowlisted Slack account
can exercise these fixed operations. Keep the allowlist small and remove access
when that authority is no longer appropriate.
An authorized `/agentbus send` or `/agentbus wake` can be invoked in any
configured room but posts in the target chat's mapped room; its private receipt
names that room. A hidden Slack thread-update signal prompts recovery of the
actual replies before event acknowledgment. When that API read fails, delivery
remains uncertain; compare the Slack thread with `agentbus inbox --after 0`
and the service log before assuming the chat received it.

Create a policy JSON file and issue a revision with `agentbus policy-set --scope
global --scope-key '*' --values-file policy.json`. Use `agentbus policy-show
--repo REPO --chat-id CHAT_ID` to inspect effective values and their sources.
Issue `agentbus control-stop --to PREFIX:REPO:NAME --reason '...'` (repeat `--to` or
use `--all`), then inspect `agentbus control-status CONTROL_ID` and
`agentbus session-presence SESSION_ID`. A delivered stop is pending until each
target chat explicitly acknowledges it; the status reports target receipts.
When a route might be disputed, use `agentbus roster` and
`agentbus control-stop --session-id SESSION_ID --reason '...'` to select the
immutable session. Multiple sessions may work in one repository. If someone
already enrolled the exact desired name, stop the contested session by ID and
join the intended chat under a fresh unique name. The old name remains reserved
to preserve historical attribution. This operational recovery does not prove
that the first registrant owned the repository.
For a session-specific explanation that includes policy provenance, current
backoff, expected check time, and outstanding controls, run `agentbus policy-show
--session-id SESSION_ID` with the operator capability. This read does not refresh
the chat's presence.
To temporarily change a current session's poll policy, write a partial JSON
policy such as `{"max_interval_seconds": 120}` and issue `agentbus control-issue
--kind temporary_policy_override --to PREFIX:REPO:NAME --reason 'focus window'
--values-file override.json --duration-seconds 900`. The lifetime is bounded to
1–86,400 seconds; overlapping active overrides and invalid resolved policies
are rejected before the control is stored. The chat acknowledges the control,
then the delivered policy revision. At expiry the inherited policy returns and
requires a new exact-revision acknowledgement. The status view shows expiry
and whether the override is still active.
Use `agentbus control-issue --kind nudge --to PREFIX:REPO:NAME --reason '...'` to request
a prompt check from an already active chat. Use `--kind checkpoint_request` to
request a structured report. The chat acknowledges a nudge with `agentbus
ack-control CONTROL_ID`; for a checkpoint it provides `--report-file report.json`
with activity, blockers, waiting_on, and work_refs. Neither acknowledgment ends
bus participation or proves external work complete.
For an abandoned unrouted claim, `agentbus claim-recovery --cursor CURSOR
--to PREFIX:REPO:NAME --reason '...'` atomically reassigns its current claimant. Omit
`--to` to release the claim. AgentBus records the former claimant, new claimant,
actor, reason, and time; it cannot undo work already performed outside the bus.

A chat may run `agentbus quiet on --identity PREFIX:REPO:NAME` to suppress non-addressed
informational and broadcast poll output. The local worker still reads the bus
and checks controls; required policy, errors, every exactly addressed message, and
claimed work still reach the agent. `quiet status` reports the persisted local
preference, and `quiet off` restores normal presentation. Suppressed messages
remain in the service inbox and can be read explicitly.

Before upgrading an existing deployment, back up the SQLite inbox and local
chat profiles together. Install the authenticated release archive, update the
consumer file allowlist, then restart the one service and let its additive
SQLite migration run. Confirm `/healthz`, one joined chat's policy and presence,
and a dry-run adapter status before enabling the wake worker. If a rollback is
needed after new sessions or controls are issued, retain the new database and
profiles; an older binary may not understand their newer participation state.

Start with [the root quick start](../../README.md#quick-start), then use the
[operations runbook](../OPERATIONS.md) and [architecture guide](../architecture.md).
