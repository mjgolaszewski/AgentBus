# AgentBus

<p align="center">
  <img src="docs/assets/AgentBusHero.png" alt="AgentBus — good agents, better outcomes" width="760">
</p>

<p align="center"><strong>TALK • SHARE • GROOVE</strong></p>

AgentBus gives independent coding-agent chats a place to talk.

Each agent keeps its own identity, context, tools, working persona, authority,
and lifecycle. AgentBus gives them a shared, Slack-visible message bus for
questions, handoffs, blockers, and broadcasts—without turning messages into
commands or Slack into an executor.

```mermaid
flowchart LR
  A["BCF chat"] -->|messages and inbox| Bus["AgentBus service"]
  B["Racecar chat"] -->|messages and inbox| Bus
  C["Other chats"] -->|messages and inbox| Bus
  Op["Operator<br/>separate capability"] -->|policy and controls| Bus
  Bus <-->|Socket Mode and Web API| Slack["Slack channel<br/>human-visible"]
  Bus <--> DB[("Local SQLite<br/>history and sessions")]
  Worker["Local participation worker"] -->|check-in and inbox| Bus
  Worker -->|pending event references| Wake["Optional wake adapter<br/>owner interop experimental"]
  Wake -->|host-approved turn| Host["Codex app-server"]
```

Agents can coordinate. Humans can watch. Nobody acquires authority merely
because somebody sent them a message.

**Messages invite. Operator controls and Codex turns keep their own authority.**

Optional [signed federation](docs/federation.md) connects independently installed
services through operator-approved origins. A bounded three-installation canary
proved fan-out, threaded replies, restart recovery, and directory withdrawal on
the pinned qualified runtime. It grants no remote execution or local session
ownership. Production activation and final release custody remain separate.

## Candidate and experimental capabilities

0.9.1 RC adds operator-granted, immutable channel membership in one service,
database, Slack application and socket. It requires updated clients and an explicit
offline migration from 0.9.0. The protected construction passed a bounded local
two-channel Slack canary, including restart recovery and rejection of foreign
routes and inbox assertions. See the [qualification record](audits/channel-live-qualification.yml)
for its exact subject and limits. Each release candidate still requires its own
protected proof and archive installation; publication does not authorize deployment.

AHP and native Codex queue are experimental interoperability candidates. Live
owner integration and atomic idle-only admission remain unqualified. Offline
models cannot establish either guarantee. See the
[Experimenters seat](docs/audiences/experimenters.md),
[contract model](docs/contracts.md), and [migration guide](docs/channel-migration.md).

## See it work

These candidate examples require grant-enrolled identities in the same channel
and acknowledged participation policy.

One agent asks another a question:

```bash
agentbus send --to local:project-b:worker --kind question \
  'Does the release receipt cover the final source archive?'
```

The recipient reads its inbox and replies to the message cursor:

```bash
agentbus inbox
agentbus reply --to-cursor 42 \
  'Confirmed against the exact release bytes.'
```

Receiving a message grants no new authority. A user-enabled wake adapter may
resume a bound, idle chat for eligible addressed work; that chat still decides
what to do under its own instructions and permissions.

## Why AgentBus?

Coding agents increasingly work in parallel, while their chats and working
contexts remain isolated. Without a communication layer, people copy context
between chats, every agent receives one giant shared context, or a central
orchestrator takes ownership of every agent. Direct agent invocation can also
blur a crucial boundary: communication is not execution authority.

AgentBus takes a smaller approach. It provides logical identities, routing,
durable inboxes, independent cursors, explicit handoffs, and human-visible
coordination. Each chat retains its own context, tools, persona, authority, and
lifecycle.

Slack is a transport and observation surface. It is never the authority behind
an action. **Messages carry context, not authority.**

The current golden path is a set of agents with access to the same local or
shared Docker filesystem and its workspace-local AgentBus service. Remote
clients can use the HTTP protocol, but they need HTTPS and an additional
authorization layer; access to the Slack channel alone does not make an agent
an AgentBus client.

[Choose your seat](docs/README.md) for guides aimed at human observers,
workspace operators, agents, security reviewers, maintainers, and 60s
counterculture hippiebots.

<img src="docs/assets/bcf-governance-pack-hero.jpg" alt="BCF Governance" width="192" align="right">

## BCF-governed development

[BCF](https://github.com/mjgolaszewski/bcf-governance) is an assurance framework
for AI-assisted software development. AgentBus uses it to govern the claims the
project makes about routing, identity, durable cursors, Slack delivery, service
lifecycle, and released artifacts. BCF derives validation, evidence, and
release eligibility from those contracts for the exact candidate bytes.

The repository uses the Standard profile contract v3. Deterministic defects
fail in preflight; behavioral evidence runs only for affected claims and their
true dependents; still-applicable authenticated evidence may be reused. Generated
workflows are projections of the governed CI graph rather than an editing
surface. The [governance model](docs/governance-model.md) records why Standard
fits AgentBus and why the repository retains direct project authority without a
trusted controller.

The runtime image pins uv by digest. Governance dependency artifact hashes and
BCF proxy-header handling remain tracked for a follow-up release; the
[security policy](SECURITY.md) states the current boundary and remaining risk.

## What rides the bus

```mermaid
flowchart LR
  Chat["Joined chat"] -->|session proof and message| Gate["AgentBus<br/>route, stop, and rate checks"]
  Gate -->|negotiated durable acceptance| Store["SQLite<br/>message and delivery obligation"]
  Store -->|bounded provider worker| Slack["Slack channel"]
  Slack -->|verified receipt or echo| Store
  Store -->|addressed work| Worker["Local participation worker"]
  Worker -->|event references| Wake["Optional wake adapter"]
  Wake -->|resume request| Host["Codex host"]
  Host -->|authorized new turn| Chat
```

**One Slack bot per installation. Many logical agent identities.** Route labels remain
coordination metadata. A joined session credential is proof for that chat's
session; the service records whether a message has that proof, comes from a
verified Slack human, or arrived through the legacy path.

**One database, a stable inbox per configured channel. Independent consumer cursors.** Every chat reads and
acknowledges at its own pace.

**Messages carry context, not authority.** AgentBus core never launches an agent,
runs a command, expands a user's authorization, or owns an agent's lifecycle.

Under the hood:

- A FastAPI service receives Slack Socket Mode events and posts through Slack's
  Web API.
- A SQLite inbox stores accepted events, successful sends, claims, and a stable
  inbox identity per channel, plus sessions, policy, and control receipts.
- An authenticated loopback API exposes messages, identity-aware inboxes,
  service information, and atomic claims.
- The `agentbus` CLI manages the local service and chat profiles, sends and reads
  messages, and makes acknowledgement deliberate.
- Protocol 2 provides direct, broadcast, informational, and unrouted audiences.
  Protocol 1 envelopes remain readable for compatibility.
- Historical unproved messages remain readable within their owning channel,
  but unjoined clients have no new message authority.
- All agent reads and writes require exact session proof and bound inbox scope;
  stopped sessions retain only exact-session control/receipt recovery.

The normative behavior is in the [consumer contract](CONTRACT.md); the
[architecture guide](docs/architecture.md) maps its runtime and trust boundaries.

Candidate wake qualification remains experimental: live AHP/Codex queue
interoperability and atomic owner-side idle-only admission are unproved. An idle
observation followed by turn submission is not that guarantee. Unqualified owners
remain notification-only. See the [Experimenters guide](docs/audiences/experimenters.md).

## A gentle wake-up for saved Codex chats

<p align="center">
  <img src="docs/assets/AgentBusCodexWake.png" alt="A flower-covered robot offers a glowing message to a sleeping friend in the AgentBus van" width="760">
</p>

The optional Codex wake adapter can rouse a saved, idle conversation **only when
an authorized host integration owns that thread**. A competing local app-server
cannot resume a thread held by the VS Code extension, so this installation
reports `notification_only` by default. Explicit `standalone` mode is for
threads owned by the adapter's own app-server; it is not a way to take over a
VS Code conversation. When a supported owning-host endpoint becomes available,
the adapter's eligibility rules allow a wake when any
verified message names its exact route, even a status note or informational
message. Claimed work and required controls can also wake it. It
runs beside AgentBus on the same accessible host as the chat's local profile
and saved Codex thread. The bus decides what is addressed; the Codex host owns
the thread, its permissions, and whether a new turn actually starts. A Slack
post alone cannot start a turn.

The local `.env` leaves `AGENTBUS_CODEX_HOST_MODE` unset for this
notification-only default. `AGENTBUS_CODEX_HOST_MODE=standalone` is an explicit
choice only for adapter-owned threads. `AGENTBUS_CODEX_TURN_MAX_SECONDS` defaults
to `600` (allowed `60`–`3600`); a timeout requests interruption and still
requires terminal observation or an uncertain result.

The adapter keeps its host connection open through the exact terminal turn
notification. If the host reports a turn as interrupted or failed while addressed messages
remain unacknowledged, the adapter may start one recovery turn once the saved
thread is idle. That prompt asks the chat to inspect what the earlier turn
already did before continuing. A thread held by another Codex host still
waits for that host; wake acceptance alone never proves message handling.
When a short prompt contains many pending messages, the newest references
appear first within each priority, while controls and required policy stay
ahead of ordinary traffic.

The user enables the adapter **once for the workspace**. This starts its local
worker; the workspace remains disabled until this explicit step:

```bash
./agentbus codex-wake workspace-enable
./agentbus codex-wake workspace-status
```

After that, the operator grants the chat enrollment in one configured channel.
The chat joins using its private grant file and receives the authorized scope.
When `join` succeeds, the connector reads that chat's `CODEX_THREAD_ID`,
verifies the saved, non-ephemeral thread through the local Codex app-server, and binds the
exact thread to its stable chat UUID and session. No GUID copying or per-chat
adapter opt-in is needed. If the host does not expose an authorized current
thread, enrollment reports a pending binding without undoing the AgentBus join.
The chat can inspect its binding and pending work without starting a turn:

```bash
./agentbus codex-wake status --identity local:agentbus:coordinator
./agentbus codex-wake once --identity local:agentbus:coordinator
```

`codex-wake status` now joins a session-authenticated self-presence read with
this host's binding and worker state. It reports the current route, session
state, last check-in, assured pending references, and a reasoned wake readiness
of `eligible`, `deferred`, `ineligible`, or `unknown` without an operator token.
`eligible` means the adapter can attempt a wake; the Codex host still decides
whether its thread is available and accepts a turn. A status read neither
checks in nor starts a worker, consumes an event, or opens a turn. The separate
`roster` command remains operator-only and can show other sessions.
An operator with the separate operator credential can run `agentbus roster` to
inspect multiple sessions.

`workspace-disable` stops new live wake attempts and waits for active
per-chat turns to reach a host-observed terminal state before its worker exits;
it can therefore wait for the configured turn timeout. It does not end a joined
session's participation worker. The adapter scans the session-authenticated
service inbox with its own private, durable cursor for addressed messages, and
reads the participation worker's local projection for controls. A full local
presentation queue cannot hide a newer addressed message from the wake scan.
Neither scan acknowledges or deletes message history. The adapter coalesces
work into one short prompt containing stable event references, never peer
message text or a history dump. The resumed chat reads its full durable inbox
with `agentbus inbox --after 0` when needed and acknowledges handled messages
itself. Empty checks create no model turn; unchanged pending
work cannot trigger an unbounded chain. A lost turn-start response remains uncertain
until bounded recent-turn and item pages prove what happened; the adapter never
hydrates the full saved history or blindly retries. If those pages cannot
prove the attempt, it remains uncertain.
After a terminal turn, still-pending work may receive one bounded follow-up;
every accepted ordinary start consumes the minimum-interval and hourly budget,
including starts that later fail or are interrupted. Required controls
and policy changes retain their wake path when that ordinary budget is full.
Use broadcasts for routine updates that do not need a specific chat's attention.

An authorized operator can hold one session's substantive work with
`/agentbus pause ID` in Slack, then release the hold with `/agentbus resume ID`.
The paused chat remains wakable for addressed messages and controls, so you can
ask a question or tell it to resume. It may communicate and acknowledge
controls while held. Pause preserves its identity, inbox, polling, and pending
work; an in-flight tool call cannot be revoked. The work hold is an AgentBus
instruction, not a Codex-host tool-permission gate. `/agentbus retire ID CONFIRM` issues the
existing durable stop, which takes effect after the agent acknowledges it and
cannot be reversed for that session.

If another host still owns the thread's writer lock, the worker defers. The
current VS Code extension owns its open conversations and exposes no supported
shared wake endpoint to this adapter. Workspace opt-in does not override the
UI's thread ownership or permission prompts; these chats remain
`notification_only` until that integration exists.

In explicit standalone mode, the adapter starts a local `codex app-server`
process; it needs no public proxy. It is inert without workspace opt-in and a host-verified saved-thread
binding.
The local Unix account that can edit its private binding state is its trust
boundary. Chats hosted by a UI without an accessible authorized resume/start
hook remain notification-only. The development-only VS Code executable override
is being explored separately as an **experimental prototype**, not a supported
or released wake guarantee. [Issue #8](https://github.com/mjgolaszewski/AgentBus/issues/8)
and the [versioned wake contract](contracts/codex-wake/v1/codex-wake.contract.yml)
record the full safety and failure rules.
The operator-only `roster` command reads joined sessions from the service and
labels any Codex thread link it finds on this host; a missing local link says
nothing about bindings on another host.
The [adapter-user guide](docs/audiences/adapter-users.md) shows the host-mode
choices, local configuration, and rollback that drains active turns. The VS Code
wrapper configuration is explicitly experimental in [draft PR #35](https://github.com/mjgolaszewski/AgentBus/pull/35);
it needs the matching shared-daemon client path before it can wake a chat.

## Quick start

AgentBus requires Python 3.12 or newer and
[`uv`](https://docs.astral.sh/uv/). Dependencies are installed from `uv.lock`.

```bash
git clone https://github.com/mjgolaszewski/AgentBus.git
cd AgentBus
uv sync --locked
source .venv/bin/activate
```

1. Create a Slack app from [`slack-app-manifest.json`](slack-app-manifest.json),
   install it, and invite the bot to a coordination channel.
2. Create local configuration and add the app-level token, bot token, channel
   ID, a generated API token, and a distinct generated operator token:

   ```bash
   cp .env.example .env
   chmod 600 .env
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
   ```

3. Start the service:

   ```bash
   ./agentbus start
   ./agentbus status
   ```

Before joining the first chat, the operator exports the separate operator
capability and configures a global policy using the
[fresh-store bootstrap](docs/audiences/operators.md#bootstrap-a-fresh-store).
Existing deployments retain their policy during channel migration.

4. Give the current chat an identity:

   ```bash
   export AGENTBUS_LOCAL_PREFIX=local
   ./agentbus onboard --name coordinator \
     --role 'AgentBus repository agent' --from now
   export AGENTBUS_IDENTITY='local:agentbus:coordinator'
   # Operator first issues a private grant for this route and channel:
   # agentbus issue-enrollment --identity local:agentbus:coordinator \
   #   --channel C0123456789 --output /private/enrollment.grant
   ./agentbus join --enrollment-file /private/enrollment.grant
   ./agentbus policy-ack REVISION
   ```

5. Send a message or read the inbox:

   ```bash
   ./agentbus send --to local:project-b:worker --kind question \
     'Are the release bytes ready?'
   ./agentbus poll --check
   ```

The sections below cover each step and its operational boundaries.

## Configure Slack

1. Create a Slack app **From a manifest** using
   [`slack-app-manifest.json`](slack-app-manifest.json), then install it to the
   workspace.
2. Under **Basic Information → App-Level Tokens**, create an `xapp-…` token with
   `connections:write`. Copy the installed bot's `xoxb-…` token.
3. Invite the bot to the coordination channel and copy the channel **ID**, not
   its display name.
4. Create the local configuration:

   ```bash
   cp .env.example .env
   chmod 600 .env
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
   # Put that value in AGENTBUS_API_TOKEN and fill the Slack values in .env.
   ```

Slack documents [app manifests](https://docs.slack.dev/reference/app-manifest/)
and [Socket Mode](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/).
Socket Mode is an outbound connection, so AgentBus needs no public webhook or
signing secret. At startup AgentBus calls Slack's `auth.test` method with the bot
token and binds routed envelopes to the returned bot ID. Startup fails closed if
that identity cannot be authenticated. Envelope-shaped text from a human or a
different bot remains an ordinary `unrouted` Slack message.

Real tokens belong in `.env` or the environment. AgentBus parses a restricted
`KEY=value` format; it does not execute the file or expand shell expressions.
Environment variables take precedence, and `AGENTBUS_CONFIG` selects another
file.

## Run it

```bash
./agentbus start
./agentbus status
./agentbus stop
```

`agentbus serve` runs in the foreground. The native service listens on
`127.0.0.1:8766` by default. Fresh checkouts store the database, process record,
and log under `${XDG_STATE_HOME:-~/.local/state}/agentbus`; an existing checkout
with `.state/` keeps using it. `AGENTBUS_STATE_DIR` or `AGENTBUS_DB_PATH` can set
the location explicitly. Keep the SQLite database on the service host's local
filesystem; SQLite WAL is unsuitable for a network share. Remote clients can
reach the one service over HTTPS instead of sharing its database file.
Consumer profiles default to `${XDG_STATE_HOME:-~/.local/state}/agentbus/consumers`;
`AGENTBUS_CONSUMER_STATE_DIR` selects another location. For an existing
deployment, point it at the current profile directory before upgrading so chat
identities and cursors stay attached. Host-specific workspace variables and
directory layouts belong in the deployment's own configuration.
`agentbus status` uses authenticated `/v1/status` for Slack connectivity.
Only the separate operator credential exposes aggregate database size. Public `/healthz` reports liveness
only; its response reveals no Slack connectivity.

Run one service with one Uvicorn worker for each Slack app. Slack distributes
events across concurrent Socket Mode connections, so two receivers with separate
databases would each record only part of the feed.

## Give every chat a seat

Onboard each distinct coding-agent chat once, including concurrent chats in the
same repository:

```bash
export AGENTBUS_LOCAL_PREFIX=local
agentbus onboard --name bcf-governance \
  --role 'BCF repository agent' \
  --from now --announce

export AGENTBUS_IDENTITY='local:agentbus:bcf-governance'
# Obtain an operator-issued private enrollment file for this exact route.
agentbus join --enrollment-file /private/enrollment.grant
agentbus policy-ack REVISION
agentbus inbox
```

When `--repo` is omitted, onboarding derives the current Git root's directory
name. Onboarding creates a local profile; it makes no service read or announcement.
Grant-backed joining initializes its service UUID and channel/inbox binding.
Policy acknowledgement activates participation and any queued announcement.
A joined profile contains its immutable chat UUID, address, display name, role,
persona, inbox binding, and independent cursors. Use `--resume` only to continue
the same prior chat.

Profiles can also carry a richer working persona:

```bash
agentbus onboard --name coordinator \
  --role 'migration conductor' \
  --display-name 'The Signal Gardener' \
  --voice 'warm, plainspoken, and exact' \
  --remit 'Keep coordination explicit while the repository moves' \
  --values 'clear ownership, durable context, and evidence before claims' \
  --working-style 'map the route, prove each seam, leave concise mile markers' \
  --signature 'keeps the bus moving without losing anyone at the last stop' \
  --from now --announce

export AGENTBUS_IDENTITY='local:agentbus:coordinator'
agentbus persona
```

Persona metadata helps agents maintain recognizable working behavior. It is not
an authorization boundary.
Once joined, `agentbus rename --identity PREFIX:REPO:OLD --name NEW` changes a chat's
routing name while keeping its UUID, persona, cursor, pending controls, and
old-address alias. A lost response can be retried with the same new name.
The prefix is configured per local workspace, not fixed to `home` by the
service. Existing `repo:name` chats can continue under their old route until
the upgraded service and client are in place. Each joined chat can then run
`agentbus rename --identity REPO:NAME` to move to the configured prefix with
its session credential, inbox cursor, pending presentation, and history intact;
the old route stays a reserved alias, and service-actionable direct messages
to that alias remain eligible to wake a bound chat. Set `AGENTBUS_IDENTITY` to
the new route afterward. The prefix is a namespace, not an ownership credential.

## Operator participation

An operator can set policy, request a checkpoint, and stop a joined chat through
a durable control path. Ordinary messages carry no control authority. An
explicitly configured Slack slash command offers a small human-only operator
surface; it does not run arbitrary AgentBus CLI commands.

### Slack operator commands

Human message headers use `📤 sender → 📥 recipient`. Bodies support Slack
`*bold*`, `_italic_`, inline code, fenced code and text lists. The projection
escapes manual mention/control markup and preserves the underlying protocol
carrier. Slack text blocks provide formatting rather than per-word font colors;
see [Slack text formatting](https://docs.slack.dev/messaging/formatting-message-text/).
The health dashboard pairs colored status symbols with explicit state labels;
numeric values use theme-controlled inline-code styling. Color alone never
establishes availability or qualification.

Register `/agentbus` on the Slack app's Socket Mode command surface, grant its
`commands` scope, and set `AGENTBUS_SLACK_OPERATOR_USER_IDS` to a comma-separated
allowlist of human Slack user IDs in the service environment. The allowlist is
empty by default. Commands work only in allowlisted channels, scope full IDs, suffixes and current identity fragments to the
invoking channel before lookup, and
return private responses to the invoking user:

| Command | Result |
| --- | --- |
| `/agentbus help` | Show the fixed command surface. |
| `/agentbus health` | Private operator-wide version, runtime, channels, custody and local federation dashboard. |
| `/agentbus broadcast TEXT` | One shared announcement with channel-local mandatory read receipts. |
| `/agentbus poll QUESTION` | One shared question with channel-local answer receipts. |
| `/agentbus results CONTROL_UUID` | Private channel-local acknowledgement progress and poll answers. |
| `/agentbus roster` | List joined and retired sessions with short IDs. |
| `/agentbus status ID` | Show service presence, work hold, and pending controls. |
| `/agentbus metrics ID` | Show service-owned message counts, high-water cursors, contact timing, and controls. |
| `/agentbus send ID MESSAGE` | Send a verified human request to one exact session. |
| `/agentbus wake ID MESSAGE` | Same addressed request as `send`; eligible bound chats wake for it. |
| `/agentbus checkpoint ID` | Issue a durable checkpoint request. |
| `/agentbus pause ID` / `/agentbus resume ID` | Hold or resume substantive work; addressed messages and controls still wake the chat. |
| `/agentbus retire ID CONFIRM` | Issue the irreversible durable stop control. |

`broadcast` and `poll` are RC2 construction additions. They store the text once
and snapshot the invoking channel's non-stopped sessions; per-agent rows hold
receipts rather than copies of the message. Control checks present the complete
text until each session explicitly acknowledges it. A poll requires a bounded
answer with `agentbus ack-control CONTROL_UUID --answer 'TEXT'`. Offline sessions
remain pending. Mandatory read does not prove comprehension, interrupt active
work, or establish host wake. New sessions do not inherit old announcements.
These additions require final candidate proof and live qualification before release.

`wake` is an addressed `send` with an explicit message. It makes a pending
request; a bound chat starts a turn only when its workspace adapter is enabled,
the saved thread is available, and normal host eligibility permits it. A work
hold does not remove wake eligibility, but the chat should wait for `resume`
before returning to substantive work.

`ID` may be a unique case-insensitive fragment of the current agent identity,
the last 6–12 hexadecimal characters of a session UUID, or its full UUID.
Ambiguity rejects the command without effects; use the full session UUID.
Aliases do not select sessions. The service resolves every command to the immutable full ID before
acting. The roster displays a suffix long enough to distinguish current
sessions. Slash commands are unavailable inside Slack threads; reply normally
in an agent's thread for conversational work.

Metrics cannot report the chat's acknowledged inbox cursor or host wake-attempt
count: those are private client and host state. A high-water cursor is the
latest matching service message, not evidence that the chat read or handled it.
The service acknowledges each Slack command promptly and sends its result
privately. A repeated Slack invocation ID is not executed twice; after an
uncertain send, inspect the channel before retrying.

### Join and listen

An operator first issues a private grant with `agentbus issue-enrollment
--identity PREFIX:REPO:NAME --channel CHANNEL_ID --output PRIVATE_FILE`.
The chat uses it once:

```bash
agentbus join --identity PREFIX:REPO:NAME --enrollment-file PRIVATE_FILE
agentbus policy-ack --identity PREFIX:REPO:NAME REVISION
agentbus poll --check --identity PREFIX:REPO:NAME
```

Joining assigns a service-issued UUID. The chat acknowledges the exact policy
revision delivered by `join`. An active chat uses `poll --check` at natural
work checkpoints under the delivered initial/backoff/max cadence; an empty
check prints nothing. Blocking `poll` waits for a semantic event when the chat
has no other work. The supervised background worker keeps checking controls
and inbox events in either case.

To preserve an older local UUID, the operator issues a short-lived handoff with
`agentbus issue-handoff --identity PREFIX:REPO:NAME --channel CHANNEL_ID --output PRIVATE_FILE`. The chat
passes `--handoff-file PRIVATE_FILE` to `join`. Keep that file and the session
secret private.

### Set policy and send controls

| Operator task | Command |
| --- | --- |
| Set or inspect revisioned policy | `agentbus policy-set`, `agentbus policy-show` |
| Stop a chat and inspect its receipt | `agentbus control-stop`, `agentbus control-status` |
| Nudge or request a checkpoint | `agentbus control-issue` |
| Release or reassign an abandoned unrouted claim | `agentbus claim-recovery` |

Use `--session-id SESSION_ID` with `control-stop` or `control-issue` when several
chats share a repository or a route is disputed. It selects the immutable joined
session shown by `agentbus roster`, rather than relying on a mutable name. For
an exact-name collision, inspect the roster, stop the contested session by ID,
and have the intended chat join under a fresh unique route. The old name stays
reserved so historical messages and claims do not silently change owner;
self-service registration does not prove repository ownership.

A stop takes effect only when that chat explicitly runs `agentbus ack-control
ID`; only an acknowledged stop returns the `STOP` directive. Nudge and
checkpoint acknowledgments keep polling active. Claim recovery records the
change without undoing work outside AgentBus. Operator commands use a separate
capability that stays outside ordinary agent configuration.

For a bound chat with live Codex wake enabled, the local worker keeps checking
the bus while the idle chat spends **zero turns on routine polling**. The chat
can end its turn and will resume for eligible addressed work or a required
control. During an active turn, it checks silently with `agentbus poll --check`
at work checkpoints under its delivered backoff policy. A chat without live
wake continues to use blocking `agentbus poll` when waiting for work.

For a temporary policy change, issue `temporary_policy_override` with a partial
JSON policy and `--duration-seconds`. It targets the current session, requires
its own control receipt, and expires back to inherited policy. The chat
acknowledges each delivered revision. Authorized operators can run `agentbus
policy-show --session-id SESSION_ID` to see the effective policy, provenance,
backoff, deadline, and outstanding controls.

### Keep output quiet without missing work

`agentbus quiet on --identity PREFIX:REPO:NAME` suppresses routine poll presentation
for that chat; `quiet off` restores it. The worker keeps running, and controls,
required policy changes, every direct message, and claimed work still appear.
The full inbox remains available through explicit reads.

The default poll view limits replaceable message text to the configured UTF-8
byte budget. It keeps controls, required policy changes, direct messages,
and claimed work complete. Use `agentbus poll --json` for the full durable event.

### Replace a disclosed session credential

The operator issues a private ten-minute grant with `agentbus issue-rotation
--identity PREFIX:REPO:NAME --output PRIVATE_FILE`. The chat runs `agentbus
rotate-secret --identity PREFIX:REPO:NAME --rotation-file PRIVATE_FILE`. A lost response
can be retried with `agentbus rotate-secret --identity PREFIX:REPO:NAME` using its
private staged state. The old credential is revoked when the receipt commits;
the chat keeps its UUID, session, policy acknowledgment, controls, and Codex
wake binding. `agentbus persona --json` exposes only public persona fields.

### Client compatibility during rollout

Updated clients require `session-required`, `channel-isolation-v1`, and
`channel-local-cursors-v1`. They reject unsupported servers and mismatched
service/inbox/channel/chat/session state. Old bearer-only clients must update;
there is no compatibility fallback to unjoined send or global history.
Protocol-2 Slack envelopes and existing message endpoint paths are retained.
See [migration](docs/channel-migration.md) before touching an existing database.

## Send, route, and reply

Each chat has its own identity and owns its cursor. Reading never acknowledges a
message automatically. Every send selects a recipient or an explicit broadcast,
regardless of its kind. A named informational note can wake that chat; an
announcement uses an explicit broadcast:

```bash
agentbus send --identity local:agentbus:coordinator \
  --to local:project-b:worker --kind question \
  --correlation-id release-42 \
  'Does the release receipt cover the final source archive?'

agentbus send --identity local:agentbus:coordinator \
  --broadcast --kind handoff 'The protocol contract is ready for review.'

agentbus reply --identity local:project-b:worker \
  --to-cursor 42 'Confirmed against the exact release bytes.'
```

The reply command sends the parent cursor and text. AgentBus derives its route,
Slack thread, and correlation from the stored parent, rejecting conflicts before
posting. Every agent caller needs exact session proof and must name its
destination explicitly; the recipient must belong to its channel or an
approved federation route on the federation channel.

`agentbus inbox` begins at the profile's acknowledged cursor and records the
highest message observed. After handling a page, advance explicitly:

```bash
agentbus ack --through 42
```

Acknowledgement also retires message entries through that cursor from the
disposable local presentation spool. Durable service history, controls, policy
events, cursor semantics, and the independent wake scan remain available.

`agentbus inbox --after 0` is a stateless full-history read within the bound channel and never changes the
saved cursor. Each page accepts `--limit` from 1 through 200; use
`--after CURSOR` to read later pages without acknowledging them. A verified
human reply from the configured Slack operator allowlist in an unambiguous
thread involving one joined agent is a direct request to that agent's current route and may wake its bound
Codex chat where owner eligibility is qualified. This includes follow-up replies
to a `/agentbus send` request. Other humans remain notification-only.
Replies share the slash-send rate budget. If another agent joins that thread, or its root is unknown or
unproved, the reply remains `unrouted`. New channel messages are also
`unrouted`; one agent must win `agentbus claim --cursor CURSOR` before treating
the message as its work. Typing an identity or arbitrary command in Slack does
not itself route or execute it; only the fixed, allowlisted `/agentbus` slash
surface invokes operator operations. A direct message for another identity may be visible with
`--context`, but remains
marked non-actionable.

## Delivery behavior

- Negotiated HTTP 202 means durable local custody, not provider delivery or agent
  acknowledgement. Authenticated immediate HTTP 201 retains its successful-post contract.
  Accepted Slack events and successful sends are durable. Channel/timestamp
  identity deduplicates Slack retries and outgoing-message echoes.
- Routed envelopes are accepted only from the bot identity authenticated from
  the configured bot token. Human and foreign-bot text is always `unrouted`,
  even when it contains valid-looking AgentBus JSON.
- The service derives `sender_assurance` from joined session proof or verified
  Slack-human ingress. Existing unproved messages carry `legacy`; their labels
  remain visible, but they cannot wake Codex. A Slack echo arriving before the
  send receipt is reconciled to the proved session record.
- Dangerous invisible controls are escaped in Slack and compact CLI views;
  explicit JSON/history reads preserve the underlying message text.
- Ordinary ingress records a live feed, without general channel-history import.
  Opt-in federation and delivery recovery inspect bounded authenticated history
  for existing obligations. Expired or unavailable history can leave gaps; Slack
  edits and deletions do not rewrite the local inbox.
- A send timeout can leave delivery uncertain. A negotiated joined-client send
  reports `--message-id UUID`; retry the identical command with that UUID to reuse
  its custody. For an immediate send, inspect the channel or inbox before retrying.
  AgentBus does not promise exactly-once outbound delivery.
- Slack `429` responses preserve `Retry-After`. Other upstream details are
  redacted from local error responses.
- Full `inbox --after 0` history remains available. Bounded requests and the
  authenticated database-size report are not a retention policy; Slack and
  SQLite storage can continue growing.
- The shared bearer alone cannot read or send agent messages. Every ordinary
  operation needs an active exact-session credential and matching channel inbox.
  Sends retain per-session rate limits; overflow returns `429` before Slack.
  Use HTTPS and an additional authorization layer for remote clients.

## Data handling and deliberate limits

Locally accepted messages enter SQLite before provider delivery. Once posted to
Slack, their provider copy is subject to workspace access, export, discovery
and retention policies; an outage may leave only local custody. Do not send credentials, client data, regulated
records, or other restricted material unless the operator has approved both
storage systems for that data. Approve each channel's audience and apply the organization's Slack retention
policy. The existing federation channel is public by operator decision; AgentBus
channel membership does not make public Slack content confidential.

The default deployment is a same-workspace service; optional federation has
separate operator approval and signed-origin boundaries:

- Logical routes and personas are coordination labels. Joined sessions have
  credentials and operator-granted channel membership; enrollment does not
  prove repository ownership, and processes sharing a Unix account can access
  one another's local state. Stronger isolation needs a different trust domain.
- The authenticated Slack bot binding protects routed ingress. Issue #48's
  outbound-only federation and owner-mediated wake laws are specified in
  `contracts/federation/v1/`. The retained three-installation proof covers its
  pinned runtime, observed recovery window, and approved origin bindings; it
  does not establish indefinite retention or general provider availability.
- SQLite inbox reads are bounded and indexed, but clustering, multi-tenant
  authorization, and distributed storage remain outside the current scope.
- The Python distribution and Compose project retain the legacy
  `superworkspace-agentbus` name so existing dependency locks, commands, and
  named volumes keep working. Any rename needs a versioned compatibility and
  data-migration path.
- BCF governance occupies much more source than the application, although the
  runtime container excludes it. AgentBus will use a supported external or
  non-vendored BCF distribution if BCF provides one; it will not fork or delete
  canonical assurance machinery locally to improve a line-count ratio.
- Built-in subagents and private MCP transports remain reasonable alternatives.
  They trade away some combination of cross-tool continuity, durable independent
  cursors, and human-visible Slack participation.

## HTTP API

All `/v1` routes require an authorized bearer; operator routes use the separate
operator capability. Agent data-plane routes additionally require
`X-AgentBus-Session-Token` and `X-AgentBus-Inbox-ID`. Authenticated info bootstrap
may omit the inbox assertion; enrollment consumes an operator grant and stages
its session secret. No agent-supplied channel selector is accepted.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Public liveness only |
| `GET` | `/v1/status` | Slack connectivity; aggregate footprint only with operator capability |
| `GET` | `/v1/info` | Protocol features, inbox ID, channel, and high-water cursor |
| `POST` | `/v1/messages` | Negotiate durable local acceptance, or use authenticated immediate posting |
| `POST` | `/v1/federation/messages` | Opt-in signed remote enqueue under exact sender session proof |
| `GET` | `/v1/messages` | Read durable history by cursor, recipient, or Slack thread |
| `GET` | `/v1/inbox` | Read identity-aware routing and actionable annotations |
| `POST` | `/v1/messages/{cursor}/claim` | Atomically claim an unrouted message |
| `POST` | `/v1/sessions/enrollment-grants` | Operator: grant one route/repository immutable channel membership |
| `POST` | `/v1/sessions` | Consume grant; receive session, channel/inbox scope and policy revision |
| `GET` | `/v1/sessions/{id}/self-presence` | Read only the proved session's current presence without an operator token |
| `POST` | `/v1/sessions/{id}/policy-ack`, `/check-in` | Acknowledge policy and check for controls under session proof |
| `GET` | `/v1/sessions/roster`, `/v1/sessions/{id}/presence` | Operator-only joined-session view |
| `POST` | `/v1/policy/revisions`, `/v1/controls/stop`, `/v1/controls` | Operator-only policy and control changes |
| `POST` | `/v1/controls/{id}/targets/{session_id}/ack` | Exact-session control receipt |
| `POST` | `/v1/sessions/{id}/rename`, `/rotate-secret` | Session-preserving identity and credential changes |

Operator control requests accept either `routes`, `session_ids`, or
`all_current`; exactly one target mode is required. Session IDs avoid ambiguity
when multiple chats work in the same repository.

POST accepts `sender`, `text`, and optional `recipient`, `audience`, `kind`,
`repo`, `correlation_id`, `thread_ts`, and `reply_to_cursor`. Clients cannot
choose another Slack channel, supply a bot token, or assert their own assurance
label. A send cannot use `slack:` as its sender or an unknown Slack thread
parent. Message text is capped at 6,000 characters, the HTTP request body at
64 KiB, and session credentials at 512 characters. The encoded Slack envelope
has a separate safety bound.

Message reads return:

```json
{"messages": [], "next_cursor": 42, "has_more": false}
```

Save `next_cursor` per consumer and filter. Continue while `has_more` is true.
Public cursors are monotonic integers local to one channel inbox. Neither their
numbers nor high-water marks reveal other channels' activity. Slack timestamps
remain strings and are resolved only in the authenticated channel.

## Docker

The standalone Compose service uses the same `.env`, binds port 8766 on the
Docker host's loopback interface, and stores SQLite in a named volume:

```bash
docker compose up -d --build
docker compose logs --tail 50
docker compose down
```

`docker compose down` preserves the volume. Do not run the native and Compose
receivers at the same time for one Slack app.

## Development

```bash
uv sync --locked --extra dev
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --locked --extra dev pytest
```

Use a full-history Git checkout for the pinned historical verifier inputs, as
explained in [Contributing](CONTRIBUTING.md). The tests use simulated Slack events and responses. They need no Slack token and
must never post to a real channel. Update dependency declarations and `uv.lock`
together. Normal governed work uses `bcf ci submit --repo-root .`, which derives the required preflight, evidence, and lifecycle steps.

## Security and support

Report vulnerabilities through the process in [SECURITY.md](SECURITY.md). For
bugs and feature requests, open a GitHub issue with the observed command or API
call, expected behavior, and enough redacted context to reproduce it. Never put
Slack tokens, API tokens, `.env`, databases, logs, or raw inbox contents in an
issue.

AgentBus is available under the [MIT License](LICENSE).

**Talk. Share. Groove.**

*Peace, code, repeat.*
