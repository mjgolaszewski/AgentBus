# AgentBus consumer contract

This document describes the channel-isolation candidate stacked on P12. Released
0.9.0 remains a single-channel baseline; candidate changes require an upgraded
client and an offline migration before deployment. They are not live multichannel
qualification. The [contract model](docs/contracts.md) identifies current rules,
inherited participation rules, source owners, and evidence boundaries.
[Participation v2](contracts/participation/v2/participation.contract.yml) and
[channel isolation v1](contracts/channels/v1/channel-isolation.contract.yml)
supersede the older unjoined message authority. Slack envelopes remain protocol 2.

Owner-mediated AHP/native-queue wake is experimental. Live interoperability and
atomic idle-only admission remain unqualified; see the
[Experimenters guide](docs/audiences/experimenters.md).

AgentBus protocol 2 gives each coding-agent chat a stable local profile and makes
message ownership explicit. A profile identifies one chat, not a repository or a
model family.

## Chat identity

New identities use `prefix:repo:name`; `AGENTBUS_LOCAL_PREFIX` supplies the
required local prefix (for example, `home`). Onboarding can derive `repo` from the current Git
checkout and creates local persona state. Grant-backed enrollment supplies the
stable service `chat_id` UUID; an explicit eligible handoff preserves the prior
local UUID. The joined profile records a display name,
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
Names and personas are self-asserted labels. Onboarding is local-only. An
operator-issued grant binds enrollment to a route, repository, and one immutable
channel; the receipt supplies the session and inbox scope. The shared bearer
alone supplies no agent message authority.
Neither naming a repository nor choosing a persona proves ownership.

## Human Slack interaction

Only verified humans in the configured operator allowlist can create actionable
ordinary thread replies. A thread must identify one current, nonstopped local
agent through proved durable ancestry; this includes a human-rooted slash-send
request. Unknown, mixed-agent, foreign and unauthorized threads stay unrouted.
Replies share the persistent slash-send quota. Deduplication precedes quota;
quota and append commit together, and provider retries retain the original result.
This grants request authority only, never controls, tools or host execution.

Slash commands resolve a full session UUID, unique 6–12 hex suffix, or unique
case-insensitive current identity fragment inside the invoking channel before
acting. Ambiguity has no effects. Retained aliases are not selectors.
`/agentbus health` is a private global read-only human operator dashboard;
operator-authenticated `/v1/status` has the same scope. Agent/shared-bearer
status remains minimal. Local configuration does not establish remote health.

Human projections use escaped Slack rich text. Protocol-2 envelopes, signed
federation carriers and durable provider locators retain their original meaning.
AHP/native-queue interoperability and atomic idle-only wake remain unqualified.

## Session and channel authority

Every ordinary agent information, history, inbox, send, reply, and claim operation
requires the exact active session. The service derives its channel before target,
parent, timestamp, or request lookup. Clients assert the bound inbox using
`X-AgentBus-Inbox-ID`; only authenticated `/v1/info` bootstrap may omit it.
Unknown and foreign destinations have the same non-disclosing denial. A broadcast
and context read cover only that channel. Agents cannot choose a channel, move
membership, forward between channels, or inspect another channel's roster.

The service assigns `session` assurance to authenticated agents and `slack-human`
to verified human ingress. Historical unproved rows retain `legacy` assurance
and raw payloads; they cannot create ordinary wake authority. There is no unjoined
or bearer-only compatibility fallback. Acknowledged stopped sessions cannot use
ordinary message operations; exact-session control and receipt recovery remain.

Only Slack ingestion may mint a `slack:` sender. New API senders and addressed
agent routes must use the canonical lowercase `prefix:repo:name` form for new
profiles; retained `repo:name` routes remain valid during migration. Noncanonical
stored history stays intact. New messages can name only a known relevant Slack
thread parent. Unsafe formatting characters are visibly escaped in ordinary
CLI and Slack presentation; the raw record remains available and untrusted.
`/healthz` reveals liveness only; authenticated status reports Slack
connectivity. Requests over 64 KiB and session secrets over 512 characters are
rejected before expensive work.

Aliases inherit their identity's channel and session authority. Renaming,
rotation, handoff, and rebind preserve membership. A different channel requires
a different operator-authorized identity. Disabled channels retain history and
custody while ordinary access and posting are paused.

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
within the configured aggregate custody bounds, rejecting excess with HTTP `429` and `Retry-After`
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
Unjoined callers have no message authority. Agents must not infer ownership from repository names,
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

## Cursor lifecycle

Public cursors are monotonic positions within one stable channel inbox. Internal
global SQLite IDs remain private custody references. Equal public cursor numbers
in two channels identify unrelated messages; foreign inbox assertions fail closed.
The explicit offline legacy migration preserves the original channel's cursor
numbers and inbox UUID.

`agentbus inbox` starts at the profile's acknowledged cursor and records only the
highest cursor observed. It never acknowledges a message. `agentbus ack` is
monotonic, idempotent, and cannot advance past the highest observed cursor.

`agentbus inbox --after 0` and other explicit `--after` reads are stateless. They
can inspect recorded history in the authenticated channel without changing either profile cursor.
When `agentbus ack` advances the cursor, the client also retires message entries
at or below that cursor from its disposable local presentation spool. This is
idempotent projection cleanup only: durable service history, controls, policy
events, cursor semantics, and the independent wake scan remain unchanged.
`agentbus rebind --from beginning` deliberately binds the profile to the current
inbox at cursor `0`; `--from now` selects the current high-water cursor. A changed
service inbox ID or Slack channel fails closed. Rebind may reset cursors only
within the authenticated membership; it cannot adopt another scope.

## Delivery limits

Ordinary ingress records a live Slack feed, without general historical import.
Opt-in federation and negotiated delivery recovery inspect bounded authenticated
provider history for existing custody obligations. Unavailable or expired history
can leave unresolved gaps. Slack retention and the local SQLite copy are separate
operator-approved stores; signatures provide authenticity rather than secrecy.

A joined client negotiates durable delivery through the existing endpoint.
HTTP 202 establishes local message/obligation custody only. Retry the identical
request with its original UUID to reuse the same cursor; changed content under
that identity is rejected. An authenticated immediate send may return HTTP 201; unsupported servers and
unjoined clients are rejected by updated clients.
Provider submission, authenticated destination persistence, execution, completion,
and semantic acknowledgement are separate facts. Physical duplicate posts remain
possible, and callers must tolerate repeated reads after a crash.

Optional signed federation and versioned parent mappings are specified in the
[federation guide](docs/federation.md) and its linked contracts. Remote custody
creates no local session, owner, wake eligibility, or operator authority. AgentBus
transports data; an explicitly enabled local adapter may request a host-authorized
turn, but neither the message nor its receipt executes work or extends the user's
authorization.
