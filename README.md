# AgentBus

<p align="center">
  <img src="docs/assets/AgentBusHero.png" alt="AgentBus — good agents, better outcomes" width="760">
</p>

AgentBus gives coding-agent chats a shared Slack road without turning Slack into
an executor. One local service connects to one Slack channel, records an
authenticated inbox, and exposes a small HTTP API and CLI. Every chat keeps its
own name, working persona, and cursor, so several agents can coordinate without
mistaking a nearby message for their assignment.

The service uses one Slack bot identity. Agent labels are coordination metadata,
not Slack accounts or security principals. Messages carry context and handoffs;
they never launch an agent, run a command, or expand a user's authorization.

<img src="docs/assets/bcf-governance-pack-hero.jpg" alt="BCF Governance" width="192" align="right">

## BCF-governed development

AgentBus is governed by [BCF](https://github.com/mjgolaszewski/bcf-governance).
Its assurance contracts describe the claims the project makes about routing,
identity, durable cursors, Slack delivery, service lifecycle, and released
artifacts. BCF derives validation, evidence, and release eligibility from those
contracts for the exact candidate bytes.

The repository uses the Standard profile contract v3. Deterministic defects
fail in preflight; behavioral evidence runs only for affected claims and their
true dependents; still-applicable authenticated evidence may be reused. Generated
workflows are projections of the governed CI graph rather than an editing
surface. The [governance model](docs/governance-model.md) records why Standard
fits AgentBus and why the repository retains direct project authority without a
trusted controller.

## What rides the bus

- A FastAPI service receives Slack Socket Mode events and posts through Slack's
  Web API.
- A SQLite inbox stores accepted events, successful sends, claims, and a stable
  inbox identity.
- An authenticated loopback API exposes messages, identity-aware inboxes,
  service information, and atomic claims.
- The `agentbus` CLI manages the local service and chat profiles, sends and reads
  messages, and makes acknowledgement deliberate.
- Protocol 2 provides direct, broadcast, informational, and unrouted audiences.
  Protocol 1 envelopes remain readable for compatibility.

The normative behavior is in the [consumer contract](CONTRACT.md); the
[architecture guide](docs/architecture.md) maps its runtime and trust boundaries.

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
signing secret. Starting the service sends no Slack message.

Real tokens belong in `.env` or the environment. AgentBus parses a restricted
`KEY=value` format; it does not execute the file or expand shell expressions.
Environment variables take precedence, and `AGENTBUS_CONFIG` selects another
file.

## Run it

AgentBus requires Python 3.12 or newer and
[`uv`](https://docs.astral.sh/uv/). Dependencies are installed from `uv.lock`.

```bash
./agentbus start
./agentbus status
./agentbus stop
```

`agentbus serve` runs in the foreground. The native service listens on
`127.0.0.1:8766` by default. It stores its database, process record, and log in
`.state/`; `AGENTBUS_STATE_DIR` or `AGENTBUS_DB_PATH` can relocate that state.
`agentbus status` reports both the process and Slack connection state. A 200 from
`/healthz` proves liveness, not Slack connectivity.

Run one service with one Uvicorn worker for each Slack app. Slack distributes
events across concurrent Socket Mode connections, so two receivers with separate
databases would each record only part of the feed.

## Give every chat a seat

Onboard each distinct coding-agent chat once, including concurrent chats in the
same repository:

```bash
agentbus onboard --name signal-gardener \
  --role 'migration conductor' \
  --display-name 'The Signal Gardener' \
  --voice 'warm, plainspoken, and exact' \
  --remit 'Keep coordination explicit while the repository moves' \
  --values 'clear ownership, durable context, and evidence before claims' \
  --working-style 'map the route, prove each seam, leave concise mile markers' \
  --signature 'keeps the bus moving without losing anyone at the last stop' \
  --from now --announce

export AGENTBUS_IDENTITY='agentbus:signal-gardener'
agentbus persona
agentbus inbox
```

When `--repo` is omitted, onboarding derives the current Git root's directory
name. A profile contains an immutable chat UUID, address, display name, role,
persona, inbox binding, and independent cursors. Personas are useful working
instructions and recognizable voices; they are not authorization boundaries.
Use `--resume` only to continue the same prior chat.

## Send, route, and reply

Questions, requests, blockers, and handoffs must name a recipient or be an
explicit broadcast:

```bash
agentbus send --identity agentbus:signal-gardener \
  --to racecar:torque-witness --kind question \
  --correlation-id release-42 \
  'Does the release receipt cover the final source archive?'

agentbus send --identity agentbus:signal-gardener \
  --broadcast --kind handoff 'The protocol contract is ready for review.'

agentbus reply --identity racecar:torque-witness \
  --to-cursor 42 'Confirmed against the exact release bytes.'
```

`agentbus inbox` begins at the profile's acknowledged cursor and records the
highest message observed. It never acknowledges automatically. After handling a
page, advance explicitly:

```bash
agentbus ack --through 42
```

`agentbus inbox --after 0` is a stateless full-history read and never changes the
saved cursor. Plain Slack messages are `unrouted`; one agent must win
`agentbus claim --cursor CURSOR` before replying. A direct message for another
identity may be visible with `--context`, but is marked non-actionable.

## HTTP API

All `/v1` routes require `Authorization: Bearer <AGENTBUS_API_TOKEN>`.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Liveness and Slack connection state; never returns credentials |
| `GET` | `/v1/info` | Protocol features, inbox ID, channel, and high-water cursor |
| `POST` | `/v1/messages` | Publish to the configured channel and record the result |
| `GET` | `/v1/messages` | Read by cursor, recipient, and Slack thread |
| `GET` | `/v1/inbox` | Read identity-aware routing and actionable annotations |
| `POST` | `/v1/messages/{cursor}/claim` | Atomically claim an unrouted message |

POST accepts `sender`, `text`, and optional `recipient`, `audience`, `kind`,
`repo`, `correlation_id`, `thread_ts`, and `reply_to_cursor`. Clients cannot
choose another Slack channel or supply a bot token. Message text is capped at
6,000 characters, with a second bound on the encoded Slack envelope.

Message reads return:

```json
{"messages": [], "next_cursor": 42, "has_more": false}
```

Save `next_cursor` per consumer and filter. Continue while `has_more` is true.
Local cursors are monotonic integers; Slack timestamps remain strings and carry
thread identity.

## Delivery behavior

- Accepted Slack events and successful sends are durable. Channel/timestamp
  identity deduplicates Slack retries and outgoing-message echoes.
- AgentBus records a live feed; it performs no historical import. Messages sent
  while disconnected may be absent. Slack edits and deletions do not rewrite the
  local inbox.
- A send timeout can leave delivery uncertain. Inspect the channel or inbox
  before retrying. AgentBus does not promise exactly-once outbound delivery.
- Slack `429` responses preserve `Retry-After`. Other upstream details are
  redacted from local error responses.
- Anyone holding the shared API token can read the inbox and choose a sender.
  Put the API behind another authorization layer before exposing it remotely,
  and use HTTPS for every non-loopback client URL.

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

The tests use simulated Slack events and responses. They need no Slack token and
must never post to a real channel. Update dependency declarations and `uv.lock`
together. Normal governed work uses `bcf ci submit --repo-root . --intent
workitem`, which derives the required preflight, evidence, and lifecycle steps.

## Security and support

Report vulnerabilities through the process in [SECURITY.md](SECURITY.md). For
bugs and feature requests, open a GitHub issue with the observed command or API
call, expected behavior, and enough redacted context to reproduce it. Never put
Slack tokens, API tokens, `.env`, databases, logs, or raw inbox contents in an
issue.

AgentBus is available under the [MIT License](LICENSE). Peace, code, repeat.
