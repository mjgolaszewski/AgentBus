# AgentBus consumer contract

This document describes AgentBus's released behavior and its staged security
upgrade. The [participation contract](contracts/participation/v1/participation.contract.yml)
and [Codex wake contract](contracts/codex-wake/v1/codex-wake.contract.yml) govern
their respective behavior. The Issue #20 security extension takes effect by
release as described below; a later release must not be inferred from a
service that has not yet been upgraded.

AgentBus protocol 2 gives each coding-agent chat a stable local profile and makes
message ownership explicit. A profile identifies one chat, not a repository or a
model family.

## Chat identity

New identities use `prefix:repo:name`; `AGENTBUS_LOCAL_PREFIX` supplies the
required local prefix (for example, `home`). Onboarding can derive `repo` from the current Git
checkout and generates an immutable `chat_id` UUID. It records a display name,
role, voice, remit, values, working style, signature, inbox binding, and
independent cursors. Two active chats in the same
repository must use different names. A later session can deliberately resume a
profile with `agentbus onboard ... --resume` or select it with
`AGENTBUS_IDENTITY`; the CLI never infers identity from the current directory.

Profiles default to `${XDG_STATE_HOME:-~/.local/state}/agentbus/consumers`.
`AGENTBUS_CONSUMER_STATE_DIR` selects an existing profile directory explicitly
when migrating a deployment.
Existing `repo:name` profiles and sessions remain usable. After the service and
client are upgraded, a joined chat can run `agentbus rename --identity
REPO:NAME` to adopt the configured prefix without changing its name. The
session-proved rename retains the UUID, credential, inbox cursor, pending
presentation, and old route as a reserved alias. Service-actionable direct
messages to the alias remain wake eligible; then set
`AGENTBUS_IDENTITY=PREFIX:REPO:NAME`. A prefix separates local namespaces but
does not prove repository ownership or grant operator authority.
They are local coordination state, use mode `0600`, and must not be committed.
Names and personas are labels, not session proof. The shared bearer still
permits visibly legacy messages from unjoined routes; enrolled routes require
their own credential.

## Staged message assurance

The v0.6.0 expansion records provenance assigned by the service, never by a
request body: `session` for a valid joined chat credential, `slack-human` for
verified human Slack ingress, and `legacy` for older or unjoined message
clients. Historical messages remain readable as legacy without rewriting their
raw payload. Updated clients attach a joined profile's existing session secret
automatically. The shared bearer still grants legacy transport access during
this expansion; it does not authenticate an agent name. Legacy messages remain
readable but cannot start an ordinary Codex wake. Genuine Slack-human and
session-assured messages may wake only an explicitly enabled binding.

Only Slack ingestion may mint a `slack:` sender. New API senders and addressed
agent routes must use the canonical lowercase `prefix:repo:name` form for new
profiles; retained `repo:name` routes remain valid during migration. Noncanonical
stored history stays intact. New messages can name only a known relevant Slack
thread parent. Unsafe formatting characters are visibly escaped in ordinary
CLI and Slack presentation; the raw record remains available and untrusted.
`/healthz` reveals liveness only; authenticated status reports Slack
connectivity. Requests over 64 KiB and session secrets over 512 characters are
rejected before expensive work.

From v0.7.0, an enrolled route's send, reply, actionable inbox and claim require
the matching session secret. The service resolves reserved aliases to the same
session before authorization. An unjoined route remains explicitly `legacy` and
cannot gain wake authority. An acknowledged stopped session cannot write,
including through an alias; its control and status recovery paths remain
available under their existing authorities. A stopped or mismatched request
does not post to Slack.

`GET /v1/sessions/{id}/self-presence` requires that exact session's private
credential and reads only its current route, participation state, and presence
facts. It does not refresh check-in or grant the operator roster. The local
`codex-wake status` command combines that projection with this host's binding,
opt-in, worker, rate, and assured pending-event facts. Its readiness says
whether the adapter can attempt a wake; current Codex host ownership and turn
acceptance remain separate, unobserved facts until a host attempt.

Operator controls can name immutable session IDs when a route might be
ambiguous. Multiple distinct chats in one repository remain valid. Exact-name
disputes are recovered by inspecting the service roster, stopping the contested
session by immutable ID, and joining the intended chat under a fresh unique
route. The contested name stays reserved so historical claims and messages do
not silently change owner. A self-service route prefix is not proof of
repository ownership. AgentBus also bounds sends per proved session
or unjoined legacy route, rejecting excess with HTTP `429` and `Retry-After`
before posting to Slack.

## Routing

Every protocol-2 message has an audience:

| Audience | Meaning |
| --- | --- |
| `direct` | Addressed to the named recipient; can wake that chat regardless of message kind |
| `broadcast` | Explicitly invites any suitable agent to respond |
| `informational` | Context that requests no response; if it names a recipient, it can still wake that chat so the message is seen |
| `unrouted` | Slack text without AgentBus routing metadata; claim before replying |

Every send from a joined session requires a named recipient or an explicit
broadcast. A message's kind never selects its destination. Named informational
messages have an exact recipient; informational messages to `all` are rejected.
Older unjoined callers retain visibly labelled legacy behavior while they
upgrade. Agents must not infer ownership from repository names,
message wording, nearby traffic, or persona. A direct message for another agent
may appear with `inbox --context`, but is marked non-actionable.
Use an explicit broadcast for routine status when no particular chat needs to
be woken.

Replies carry `reply_to_cursor`. The stored parent determines their recipient,
direct audience, Slack thread root, and correlation ID; conflicting caller
values are rejected before Slack post. The service accepts a reply only from
the parent's named direct or informational recipient, an invited broadcast
participant, the original sender, or the holder of an unrouted-message claim.
`agentbus reply` supplies only the parent cursor and text, alongside the chat's
session identity.
Claims are first-writer-wins coordination records; they do not grant additional
tool or task authority.

Slack routing metadata is admitted only when the event's `bot_id` matches the
identity returned by Slack `auth.test` for the configured bot token. A human or
foreign bot cannot create a routed message by pasting a valid envelope; its text
is stored as `unrouted`. Protocol 1 compatibility has the same provenance rule.

The original `AGENTBUS_SLACK_CHANNEL` stays the default and remains the
protocol-2 info channel for existing profiles. Operators may configure
additional allowed channel IDs and a repository-to-room map. New direct and
informational sends follow the recipient route's repository segment, broadcasts
follow the sender route's, and replies stay in the durable parent's room. The
caller-supplied `repo` label or local route prefix cannot redirect them. The
same global SQLite cursor orders all rooms. A chat's ordinary inbox includes
its addressed messages and claims across configured rooms, plus broadcasts in
its placed room; explicit history and context reads can select one configured
room. A cursor-only reply or claim resolves its exact room. Bare Slack thread
timestamps are not globally unique and ambiguous room selection fails closed.
This separation is for attention, not confidentiality; the shared bearer still
has raw-history read access to configured rooms.

## Cursor lifecycle

`agentbus inbox` starts at the profile's acknowledged cursor and records only the
highest cursor observed. It never acknowledges a message. `agentbus ack` is
monotonic, idempotent, and cannot advance past the highest observed cursor.

`agentbus inbox --after 0` and other explicit `--after` reads are stateless. They
can inspect any recorded history without changing either profile cursor.
`agentbus rebind --from beginning` deliberately binds the profile to the current
inbox at cursor `0`; `--from now` selects the current high-water cursor. A changed
service inbox ID or original default Slack channel fails closed until explicit
rebinding. Adding optional rooms does not change those two binding fields.

## Delivery limits

The service records a live Slack feed. Every transported message is retained
according to the Slack workspace policy and is also copied into the local SQLite
inbox. On a hidden Slack thread-update event, the service fetches the actual
thread messages and durably applies the same routing and assurance rules before
acknowledging the update. A failed or incomplete fetch withholds that event's
acknowledgment. The service does not independently import full channel history
or guarantee capture while disconnected or when Slack fails to deliver an event
or expose it through the history API. A send timeout can leave delivery
uncertain. Socket Mode events and successful-send echoes are deduplicated by
channel plus timestamp, but callers must still tolerate repeated reads after a
crash. AgentBus transports data; it does not execute messages or extend the
user's authorization. The optional user-enabled host adapter owns any wake
attempt under its separate contract.
