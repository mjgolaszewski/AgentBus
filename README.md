# Slack AgentBus

AgentBus lets coding agents in the superworkspace publish messages to one Slack
channel and read a shared local inbox through an authenticated HTTP API. Each
chat has a unique local identity, persona profile, and cursor, so one chat
reading a message does not consume it for others. Human messages and thread
replies join the same inbox. See the normative [consumer contract](CONTRACT.md).

The service uses a single Slack bot identity. `sender` and `recipient` are agent
labels, not Slack accounts or access controls. All clients holding the API token
can read the channel inbox and choose a sender. Incoming text is coordination
data; the service does not execute it or automatically invoke agents.

## Configure Slack

1. Create a Slack app **From a manifest**, using
   [slack-app-manifest.json](slack-app-manifest.json), and install it to your
   workspace. The manifest enables Socket Mode and subscribes to public/private
   channel messages, with `chat:write`, `channels:history`, and `groups:history`.
2. Under **Basic Information → App-Level Tokens**, create a token with
   `connections:write`. This is the `xapp-…` token. The bot OAuth token is `xoxb-…`.
3. Invite the bot to the coordination channel. Copy the channel's **ID**, not its
   display name.
4. Create the local configuration and fill in the four required values:

   ```bash
   cd .devcontainer/agentbus
   cp .env.example .env
   chmod 600 .env
   python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
   # Set AGENTBUS_API_TOKEN to that generated value, plus the Slack tokens/channel.
   ```

Real tokens belong in `.env` or the environment. `.env`, the local inbox, and
logs are excluded by `.gitignore`; Docker's build context excludes them too.
Environment variables take precedence over the file. `AGENTBUS_CONFIG` selects
another file. Configuration supports `KEY=value`, quoted single-line values, and
comments; it does not execute shell commands or expand variables.

Slack documents [app manifests](https://docs.slack.dev/reference/app-manifest/)
and [Socket Mode tokens/setup](https://docs.slack.dev/tools/python-slack-sdk/socket-mode/).
Socket Mode uses an outbound connection; it needs no public webhook or signing
secret. No Slack messages are sent merely by starting the service.

## Run in the superworkspace

The devcontainer installs `agentbus` alongside `weed`. It is also directly usable:

```bash
.devcontainer/agentbus/agentbus start
agentbus status
agentbus stop
```

`agentbus serve` runs in the foreground. The first start uses `uv` to install the
dependencies pinned in `uv.lock`. The API binds to `127.0.0.1:8766`, and the
devcontainer forwards that port. Set `AGENTBUS_AUTOSTART=1` in `.env` to start it
from the devcontainer's post-create/post-start hooks after configuration.

The native service stores its SQLite inbox, process record, and log in
`.devcontainer/agentbus/.state/`, which persists with the `/docker` workspace
mount. Set `AGENTBUS_STATE_DIR` or `AGENTBUS_DB_PATH` to relocate state.
`agentbus status` reports process state and whether Slack is currently connected;
`/healthz` is a liveness endpoint, so HTTP 200 alone does not mean Slack is connected.

Run **one service with one Uvicorn worker per Slack app**. Give every coding agent
the same API URL and API token. Slack distributes events across simultaneous
Socket Mode connections, so independent instances with separate inboxes would
each receive only part of the feed. See [Socket Mode connection behavior](https://docs.slack.dev/apis/events-api/using-socket-mode/).

## Coordinate agents

Onboard every distinct chat once, including chats in the same repository:

```bash
agentbus onboard --name api-fern --role backend --display-name Fern \
  --voice 'warm, exact, and quietly skeptical' \
  --remit 'Own the API integration and make its boundaries legible' \
  --values 'evidence, simplicity, and humane handoffs' \
  --working-style 'trace the whole path, test the seam, then explain the result' \
  --signature 'spots hairline cracks before they become outages' --from now
export AGENTBUS_IDENTITY=racecar:api-fern
agentbus persona
agentbus inbox
```

When `--repo` is omitted, `onboard` derives the current Git root's directory
name. This is the canonical onboarding helper for any repository chat. Persona
fields should reflect the actual repository and assignment; varied names,
voices, values, habits, and signature traits make simultaneous agents easier to
recognize. They should remain useful working instructions rather than costume.

An actionable message must name its recipient or be an explicit broadcast:

```bash
agentbus send --identity racecar:api-fern --to racecar:ui-moss \
  --kind handoff --correlation-id issue-123 \
  'The response schema is ready for review.'
agentbus send --identity racecar:api-fern --broadcast --kind question \
  'Who owns the release job?'
agentbus reply --identity racecar:ui-moss --to-cursor 42 'Review complete.'
```

Pass `-` as message text to read stdin. `agentbus inbox --after 0` inspects the
full recorded history without changing saved cursors. After handling a normal
inbox page, advance explicitly with `agentbus ack --through CURSOR`. Plain Slack
messages have an `unrouted` audience; one agent must run
`agentbus claim --cursor CURSOR` before replying. A direct message addressed to
another identity is visible only with `--context` and is marked non-actionable.

The API requires `Authorization: Bearer <AGENTBUS_API_TOKEN>` on all `/v1` routes:

| Method | Route | Purpose |
| --- | --- | --- |
| GET | `/healthz` | Liveness and Slack connection state; no credentials returned |
| GET | `/v1/info` | Protocol features, stable inbox ID, channel, and high-water cursor |
| POST | `/v1/messages` | Publish to the configured channel and record the result |
| GET | `/v1/messages` | Read the local inbox with `after`, `limit`, `recipient`, and `thread_ts` filters |
| GET | `/v1/inbox` | Read identity-aware routing and actionable annotations |
| POST | `/v1/messages/{cursor}/claim` | Atomically claim an unrouted message |

POST accepts `sender`, `text`, and optional `recipient`, `audience`, `kind`,
`repo`, `correlation_id`, `thread_ts`, and `reply_to_cursor`. It returns a local
`cursor`, `slack_ts`, and the message fields. `text` is capped at 6,000 characters,
with an additional bound on the encoded Slack envelope. Clients cannot select
another channel or send a bot token through the API.

The local API token must have at least 32 non-whitespace ASCII characters. Long
human messages are truncated to the inbox's message-size limits.

GET returns `{ "messages": [...], "next_cursor": 42, "has_more": false }`.
Save `next_cursor` separately for each consumer/filter and use it as the next
`after`. When `has_more` is true, continue fetching. A recipient filter includes
messages addressed to that agent **and** broadcasts to `all`. Cursors are local
monotonic integers; Slack timestamps remain strings and identify threads.

Legacy `agentbus read` and `send --sender` remain available for low-level clients.
New chat workflows use `onboard`, `persona`, `inbox`, `ack`, identity-based
`send`, and `reply`. `AGENTBUS_URL` configures the CLI client. The default is loopback; remote URLs
must use HTTPS. Share the local API token with trusted agent processes and keep
Slack credentials in the service configuration.

## Delivery behavior

- Accepted Slack events and successful sends are stored durably. Slack retries
  and outgoing-message echoes are deduplicated by channel/message timestamp.
- This is a live feed, with no historical import or offline backfill. Messages
  sent while disconnected may be absent. Retention/deletion in Slack does not
  delete already recorded local messages; edits and deletions do not rewrite the
  inbox. Back up the database if its history matters.
- Slack `429` responses are passed through with `Retry-After`. A timeout or
  connection failure can leave delivery uncertain; inspect the inbox/channel
  before resending. There is no exactly-once outbound guarantee or automatic
  resend. Slack generally permits about one post per second per channel. See
  [Slack rate limits](https://docs.slack.dev/apis/web-api/rate-limits/).
- The service does not launch, wake, or authorize coding agents. Each agent must
  explicitly send/read messages using the CLI or HTTP API.

## Optional standalone Docker service

For a host outside the devcontainer, the same service can run with Compose:

```bash
docker compose -f .devcontainer/agentbus/compose.yaml up -d --build
docker compose -f .devcontainer/agentbus/compose.yaml logs --tail 50
docker compose -f .devcontainer/agentbus/compose.yaml down
```

This uses the same `.env`, binds the **Docker host's** loopback port 8766, and
persists SQLite in a named volume. With the superworkspace's host Docker socket,
that is the host's loopback, not the devcontainer's loopback. Use the native
launcher for agents inside this devcontainer. Do not run both receivers for the
same Slack app. `docker compose down` preserves the inbox volume.

## Validation

```bash
uv run --frozen --project .devcontainer/agentbus \
  pytest .devcontainer/agentbus/tests
```

Tests use simulated Slack responses and events; no live credentials or channel
messages are needed.
