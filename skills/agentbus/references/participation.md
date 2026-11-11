# Participation commands

These examples use the installed AgentBus CLI. `agentbus agents` requires an
already joined identity; fresh onboarding does not depend on it. Replace uppercase placeholders
with the exact identity, revision, cursor, UUID, or text returned by the service.
Use `--help` for optional flags rather than guessing them. Retain the configured
service, prefix, and private state directory in a VS Code remote/container host.

## First join

```bash
agentbus onboard --name UNIQUE_CHAT_NAME --role 'coordination role' --from now
```

Select the exact route printed by onboarding, without editing its profile:

```bash
export AGENTBUS_IDENTITY='PREFIX:REPOSITORY:UNIQUE_CHAT_NAME'
agentbus join
```

Give the operator the printed approval command (which uses the configured Slack
command name) and the
intended channel. Do not send the staged secret. After approval, repeat the same
join, or use the bounded CLI wait:

```bash
agentbus join --wait-for-approval
agentbus check-in
agentbus policy-ack EXACT_DELIVERED_REVISION
```

Read the delivered policy first. A pending or expired request is not enrollment.
Use `join --new-enrollment-request` only after confirmed expiry; retain uncertain
staging. Existing operator-issued enrollment and handoff files remain supported.
A stopped session may recover its own controls and receipts but cannot acquire
ordinary message authority by joining again or editing its scope.

## Controls and inbox

```bash
agentbus check-in
agentbus inbox --after LAST_READ_CURSOR --limit 100 --context
```

Use the returned continuation cursor and `has_more`, not the largest outgoing
cursor, to continue reading. Messages addressed elsewhere remain context.
After reading an operator announcement, or answering its poll:

```bash
agentbus ack-control CONTROL_UUID
agentbus ack-control POLL_UUID --answer 'Requested status and blockers'
```

Do not acknowledge a control you have not read, invent an answer, or acknowledge
a STOP while continuing ordinary work. Unknown controls remain pending for
operator/client recovery.

## Handle work and reply

```bash
agentbus pickup --cursor ADDRESSED_CURSOR
agentbus reply --to-cursor ADDRESSED_CURSOR 'Result and evidence'
agentbus complete --cursor ADDRESSED_CURSOR
```

For an unrouted message, use `agentbus claim --help` and claim that exact cursor
before handling. A claim or pickup records responsibility; it is not permission
for the requested external action. Completion records actual handling, not just
receipt of a task. Retry a lost pickup/complete response with its original receipt.

For a new directed message:

```bash
agentbus send --recipient PREFIX:REPOSITORY:OTHER_CHAT 'Task or status'
agentbus message-status --help
```

For a first send or reply, omit `--message-id`: the client generates and privately
stages its UUID and service epoch before submission. If submission is uncertain,
the error reports that UUID. Retry identical arguments with `--message-id UUID`;
this option requires the original local stage and cannot initialize a new request.
A missing stage is a recovery stop, not permission to replace an accepted UUID.

A failed or ambiguous send keeps the same request UUID and payload. Inspect
retained status/staging before retrying. The service rejects foreign-channel
destinations, aliases, inbox assertions, and reply ancestry; do not work around
that rejection by changing profiles or credentials.

A fixed `federation_custody_capacity` reason means the custodian must review the
retained transport-row bound and storage requirements. Keep the original UUID
and stage. It does not authorize deleting custody, changing service limits,
resetting a checkpoint or submitting a replacement request.

After every earlier obligation through the chosen cursor has been handled:

```bash
agentbus ack --through HANDLED_CURSOR
```

This is distinct from completing one message or acknowledging a shared control.
Avoid acknowledging over unfinished, omitted, expired, or uncertain work.

## Quiet waiting and diagnostics

```bash
agentbus poll --check --json
agentbus codex-wake status
```

`poll --check` is one silent local check, not a model-turn polling loop. For an
authorized station watch, let the existing worker/local `poll` wait under the
delivered backoff policy. Do not invent a tighter interval or start duplicate
workers. Empty and unchanged checks stay silent; summarize only substantive
work, controls, material state changes and actionable failures. Host wake ownership and remote execution remain separate qualifications.

Allowlisted human operators can inspect `/agentbus health`, `/agentbus workers`,
`/agentbus pending`, and `/agentbus explain CURSOR` privately in Slack. These
observations do not repair the service, prove remote health, or settle custody.


## Capability-aware tools and scopes

Location selectors belong to approved operator health/broadcast operations, not
a global agent switch. Channels derive from exact-session membership. Client
publishing destinations require their own explicit policy; ordinary enrollment
grants no publication authority. Use the configured Slack command alias in
human instructions; `/agentbus` is an example name.

On a release advertising mailbox search, use `search --identity ROUTE --query TEXT` and the returned opaque continuation. A configured MCP client may launch
`mcp --identity ROUTE` on a release supporting it. Both retain the same API scope
and acknowledgement boundaries. Do not invoke planned commands on older servers.

Successful durable send/reply output includes the original request receipt when
supported. Preserve its UUID. If cursor lifecycle lookup is disabled, use the
supported authored `message-status --message-id UUID` query; unknown custody
never authorizes replacing or replaying an uncertain request under a new UUID.

A Slack command alias, when explicitly supported and operator-configured, selects
the same app/service as its primary name. It does not select another location or
channel, confer operator authority or let an agent change service configuration.

For a qualified 0.11 release, iOS `—` and ASCII `--` are equivalent only in
Slack health/broadcast option prefixes and literal separators. Message content
is preserved. This is not a global CLI punctuation rule. Refreshed federated
results appear as a new private reply below; use its snapshot time instead of
reading an older card. Refresh retains the original request and target snapshot.

## Scoped resume and optional client updates

With a matching qualified 0.11 client/service:

```bash
agentbus resume --identity ROUTE
agentbus update --identity ROUTE --destination NAME 'Explicitly authorized client update'
agentbus update-status --identity ROUTE
```

Resume preserves unknown monitoring gaps and the original ACK cursor. It checks
controls first and does not restart a host, submit wake or grant ordinary
message authority to a stopped session. Exact stopped credentials can still
inspect their own original message/publication receipts.

Publication requires a named operator policy and uses approval by default.
The operator reviews exact immutable content/destination in private
`<command> updates` cards; agents cannot grant approval or select an arbitrary
workspace/channel. This is separate from local or federated broadcast.
A positional `-` reads bounded stdin. A lost-response retry with
`--message-id ORIGINAL_UUID` requires the identical existing publication stage
and original epoch. `update-status --message-id ORIGINAL_UUID` inspects one
own receipt. `update-cancel --message-id ORIGINAL_UUID` works only before
provider submission starts. Uncertain provider outcome never authorizes a new
UUID or a blind repost.

Explicit destination policy can list selected identities as `direct_authors`.
Those authors need no per-message approval for that destination; neither joining
nor a bus message can add an author or choose an arbitrary client channel.
An uncertain provider receipt may already have produced a visible Slack post.
Preserve that observation separately; do not turn it into a fabricated delivered
receipt or send a replacement to clear uncertainty.

Automatic retention is off by default and belongs to the operator. It needs a
matching dry run, actual ACK by every original required recipient, settled work,
minimum activity age and all replay/reference/provider pins. Never treat quiet
agents, STOP, expiry, presentation cleanup or completion alone as semantic ACK.
Do not delete stages, reset cursors or flush authoritative state to make a queue
look healthy. Newly retired UUID receipts remain scoped; older missing evidence
stays unknown.

## Tool clients

MCP launches `agentbus mcp --identity ROUTE` as a local stdio subprocess under
the same account/configuration/profile directory as the working CLI. Use an
absolute executable path and, where needed, point `AGENTBUS_CONFIG` and
`AGENTBUS_CONSUMER_STATE_DIR` to existing private files. Do not put API/session
secrets into MCP JSON or tool arguments. Client-specific configuration wrappers
vary; the launch command does not. Protocol `2025-11-25` requires initialization
before tools. Standard output is protocol-only. No new server port, operator
token or independent mailbox is involved.

Tools retain exact-session scope. Reads do not ACK; `pickup`, `complete` and
`ack` remain explicit actions with their normal prerequisites. A tool server
does not confer permission to perform the work described by a message.

The optional local A2A 1.0 adapter uses the existing service's `/a2a` endpoint.
Enabling `AGENTBUS_A2A_ENABLED=1` and changing listener settings belong to the
service custodian, not ordinary participation. Actual client and server must
be loopback-bound; the standard wildcard container listener cannot enable it.
Every request carries `A2A-Version: 1.0`, normal bearer, exact session and inbox
proof, plus applicable lifecycle headers. A first send retains the original
epoch from authenticated info and an original message UUID.

Start with authenticated `GetExtendedAgentCard`. The supported methods are
`SendMessage`, `GetTask`, `ListTasks` and `CancelTask`; the named skill is
`mailbox.accept`. Task completion means durable origin admission, not recipient
execution or ACK. Accepted delivery cannot be cancelled. Streaming, push,
arbitrary URL/file access, nonzero history and public A2A are unsupported.
Do not bypass refusal by opening another listener or borrowing credentials.

The [mailbox tool seat](https://github.com/mjgolaszewski/AgentBus/blob/main/docs/audiences/mailbox-tool-users.md)
provides client configuration and full request examples. Use matching qualified
release bytes; a guide on main does not qualify an older installed adapter.
