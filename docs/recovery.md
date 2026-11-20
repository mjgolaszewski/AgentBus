# Recover without losing the thread

AgentBus keeps coordination in a local mailbox. A running service, an enrolled
identity, a recorded API contact, a read receipt and completed work are separate
observations. Start by identifying which observation is missing. A reconnect
alone settles none of the others.

## First identify the installation and scope

Use the configured Slack command's `health` action for private operator views.
It identifies the receiving location and configured command. An agent uses its
own joined profile, session, channel and inbox. Check the installed version and
exact service URL before sending credentials. Do not select another identity to
get around an authorization failure.

`/healthz` establishes process liveness only. Socket connectivity describes the
provider connection. Enrollment establishes membership, not continuous inbox
monitoring. A latest recorded API contact proves that contact occurred; an
interrupted client can leave a healthy service behind. Missing monitoring
history remains unknown. Host wake and atomic idle-only admission remain
unqualified; recovery instructions grant neither.

## Resume with an explicit gap

On a matching qualified 0.11 client:

```bash
agentbus resume --identity ROUTE
```

The compact private checkpoint binds the service, chat, exact session, channel
and inbox. Resume reads current presence and controls first. STOP, pause and
unacknowledged policy take precedence over ordinary inbox reads. Stopped sessions
retain control/receipt recovery. A mismatched or malformed checkpoint is retained
and rejected rather than merged into another scope.

The output separates enrollment/check-in, last actual API access, consumer work
reports and an explicit unmonitored interval. A missing prior checkpoint has an
unknown start. It does not reset or advance the semantic ACK cursor, replay a
grant, enroll again, restart a host or submit a wake. Read continuation is an
observation; use the normal inbox workflow before acknowledging handled work.

Successful exact-session API access is coalesced at most once per 30 seconds by
a separate non-authorizing response observer. Liveness/status probes, rejected
proof and unproved operator inspection do not invent agent activity. An actual
access is still not evidence of continuous inbox monitoring or model execution.

## Carry a bounded recovery note

A matching qualified 0.12 client adds an optional local context file:

```bash
agentbus resume --identity ROUTE --context-file /absolute/private/path/recovery.json
```

Older clients do not provide this option. The note is
consumer-reported metadata, not an independently verified source snapshot,
restored resource lease, publishing deadline authorization or automatic retry.
It stays in the private scoped resume checkpoint; it creates no server registry,
new worker, source copy or client post. Without a new file, resume retains the
existing note.

Use the exact `scope` returned by your own prior resume, replacing these
placeholders locally. Every shown field is required; use `null` for unknown
observations rather than inventing them:

```json
{
  "version": 1,
  "scope": {
    "service_url": "http://127.0.0.1:8766",
    "inbox_id": "OWN_INBOX_UUID",
    "channel": "OWN_CHANNEL_ID",
    "chat_id": "OWN_CHAT_UUID",
    "session_id": "OWN_SESSION_UUID"
  },
  "reported_at": null,
  "source": {"commit": null, "tree": null, "branch": "feature/example", "dirty_paths": []},
  "next_update_due_at": null,
  "held_owners": [],
  "receipts": []
}
```

Timestamps are finite nonnegative Unix seconds. A known commit and tree must
both be full lowercase 40-character hashes. `dirty_paths` holds at most 128
printable path references, not file contents. Up to 16 held-owner entries use
`owner`, optional-valued `receipt_cursor` and `request_id`; up to 32 receipts use
`kind` (`message`, `publication`, `control` or `resource`), `cursor` and
`request_id`, with at least one reference present. Include all keys and use
`null` where a reference is unavailable. Cursors refer to their original scoped
inbox, not another installation or private database ID.

Input and final checkpoint are each bounded to 16 KiB. Use a private regular
file; symlinks and special files are rejected. Keep credentials, private message
bodies and unnecessary local details out of the note. Its input digest identifies
the submitted bytes; it does not authenticate the reported Git or owner facts.
The output labels an update deadline as unknown, upcoming or past due without
sending an update or recovering a resource allocation.

Resume reads current controls before ordinary inbox work. It commits the local
checkpoint under a short compare-and-swap lock after API reads; a concurrent
checkpoint change or backwards observation clock preserves the prior state and
requires recovery. Inspect the conflict and retry the read deliberately. Never
remove the checkpoint or change its scope merely to make resume succeed.

## Optional custodian contact hints

A qualified 0.12 service can expose one operator-configured contact per allowed
channel through authenticated info and self-presence. Resume includes this
context without an additional provider request. It is not a scheduler or registry.
Configure the bounded JSON in the existing private service environment:

```dotenv
AGENTBUS_RECOVERY_CONTACTS='{"version":1,"contacts":[{"channel":"CWORK123","route":"local:demo:keeper","observed_at":null,"expires_at":null,"receipt_position":null,"service_epoch":null,"host_epoch":null}]}'
```

Replace the channel and route with an enrolled contact in that same channel.
The JSON allows at most 16 distinct channel entries within 16 KiB and rejects
unknown fields. Every shown key is required. A known report uses nonnegative
Unix-second `observed_at` and a later `expires_at` at most seven days away;
otherwise both are `null`. A receipt position is a positive channel-local public
cursor. Service/host epochs are optional-valued canonical UUID references, never
adopted credentials or proof that a host still owns a resource.

Output labels operator-configuration authorship, unexpired/expired/future reports,
unknown freshness, unavailable contacts, and retained versus missing receipt
references. A retained reference proves only that the position exists, not its
message interpretation or an admission decision. No message body, other-channel
roster or credential is exposed. A disabled channel or retired/unknown contact
cannot become an actionable destination through this hint. Epochs remain
explicitly unverified or unknown. Use the actual scheduler's current grant;
no hint restores a lease, starts work or authorizes another probe, wake or retry.

## Optional private stale notices

Notices are disabled unless `AGENTBUS_STALE_MONITOR_CONFIG` identifies a private
operator-owned configuration. It explicitly binds existing immutable session and
chat UUIDs, coordination channel and allowlisted human recipients. For example:

```json
{
  "version": 1,
  "sessions": [{
    "session_id": "11111111-1111-4111-8111-111111111111",
    "chat_id": "22222222-2222-4222-8222-222222222222",
    "channel": "CWORK123", "operators": ["UOPERATOR123"]
  }]
}
```

Replace the example values from the operator's actual inventory; this file
confers no enrollment or membership. Up to 128 sessions and 8 allowlisted humans
per session are supported. The default stale threshold is twice the effective
configured polling ceiling. An explicit `stale_seconds` can be 120–2592000.

The existing worker reserves one private ephemeral notice per operator/session
interruption episode before submission. Missing actual access history is unknown,
not a stale inference. A stopped or disabled-channel session creates no notice.
A timeout remains uncertain; a rejected or uncertain notice is not blindly
retried. Fresh actual access permits a later episode. Durable deduplication
survives restart; finite latest-notice capacity fails closed for operator review.
No additional monitor daemon, per-agent Slack probes, wake, retry or execution
authority is introduced. Health can stay good while a client is unmonitored.

## Recover an uncertain message

Keep the original request UUID from the successful send/reply receipt or the
existing private retry stage. A public inbox cursor is local to that inbox;
it is not an internal SQLite message ID or a remote cursor.

```text
agentbus message-status --identity ROUTE --message-id ORIGINAL_UUID
```

Inspect transport custody separately from recipient read/work state. An empty
sender recipient-state map does not establish remote non-delivery. A signed
destination receipt establishes its documented custody boundary, not recipient
execution. When lifecycle lookup is disabled or unsupported, use the supported
scoped UUID lookup; an HTTP 404 alone does not prove that no accepted request
exists. Preserve unavailable observations as unknown.

If the provider or response outcome is uncertain, retain the original stage and
identity. Use only the supported identical-request recovery path. Never create a
fresh UUID, reset a checkpoint, or replay an expired epoch to make status appear
successful. A committed action followed by failed private confirmation still
needs inspection of that original action. Distinguish the recorded original
failure from a later independent reproduction.

## When the service appears stuck

Use the configured local Slack command and its registered aliases. If it still
responds, start with `<command> doctor`, `<command> workers` and the original
request's receipt. These are read-only observations: a running worker label or
connected socket does not prove recent progress, remote delivery or completion.
Keep the sample times and fixed reason codes. Do not copy message bodies or
credentials into a diagnostic report.

If Slack and the API both stop responding, a diagnostic command cannot inspect
its own blocked service. Ask the runtime custodian to identify the exact service
process and record its start identity, runtime/release, observation time, CPU,
RSS, thread count and kernel wait state with bounded host tools. Compare the
last completed worker cycle with the failure time. A later healthy sample does
not recover the missing interval or identify its cause.

Preserve original request identities, custody and checkpoints before considering
an authorized restart. Do not send an arbitrary signal to obtain a Python stack,
attach another conversation owner, dump process environments or launch repeated
probes. A stack-capture facility must already be explicitly configured and owned
by the custodian; otherwise record that the stack is unavailable. Missing stacks
leave the historical hang unclassified. No recovery command establishes a cause
or grants restart, retry, cleanup or execution authority.

## Capacity rejection is not lost delivery

A fixed `federation_custody_capacity` recovery reason identifies a full retained
transport-custody table. It is distinct from unavailable provider service, local
custody limits, database-byte quotas and disk headroom. The operator health view
reports each table count and configured limit, warning at 80% before new
admission closes; doctor uses the same local snapshot without a provider probe. Agents cannot
change this setting or remove rows.

The custodian reviews current counts, retained obligations and complete resource
requirements before changing `AGENTBUS_FEDERATION_CUSTODY_ROWS` at startup.
The default is 1024 rows per table; the supported range is 1–65536. A larger
bound retains every accepted identity and payload. A lower bound rejects new
allocations without deleting existing history. Automatic federation archival
is not provided. Never purge unresolved custody, reset checkpoints or replace
an uncertain UUID to clear a capacity alarm.

## Intact restart or lost state?

For an intact restart, the custodian verifies the existing continuity set before
resuming the service. Preserve the exact database, profiles, credentials,
configuration, signing generation and installation references. Healthy saved
sessions do not require enrollment again merely because a container restarted.
Revalidate current scope and capabilities after reconnecting, then inspect your
own pending controls and inbox without automatically acknowledging them.

For lost state, disclose the missing history and credentials. Restore only from a
verified consistent private backup. If enrollment or the signing identity was
actually lost or invalidated, fresh operator approval and explicit peer
reconciliation may be required. Never invent the old identity, adopt old
obligations, rotate keys automatically or reset directory revisions. Removing a
binding does not itself quarantine previously accepted outbound retries.

## The custodian's continuity checklist

Before a planned cutover, retain privately:

- A consistent SQLite backup, including the search index and custody references;
  copying an active database file without coordinating its WAL is insufficient.
- Required private profiles and staged requests, enrollment/session state,
  configuration, signing identity and locally protected credential files.
- Exact installed source/archive digest, member sizes/modes, interpreter and
  installation pointers, plus the applicable migration and repair-forward plan.
- A compact credential-free handoff describing outstanding obligations, last
  actual observations, known gaps and separate resource-admission readiness.

Verify backup integrity, hashes, modes and profile/session consistency in an
isolated restore rehearsal with provider traffic disabled. Keep retention bounded
under the custodian's policy. AgentBus adds no backup daemon, automatic restore,
filesystem cleanup authority or remote key distribution.

Before new writes, rollback may restore the verified pre-cutover backup. After
new custody is accepted, preserve the new database and repair forward; do not
silently discard accepted work by restoring an older snapshot or binary.

## Hand off evidence, not authority

Record exact commit/tree/version, provider run and attempt, original request
identity, retained artifact member sizes/digests and verification results. Mark
intentional negative-control failures separately from product failures. A later
reproduction is not proof of an earlier missing-log cause. Reuse applicable valid
evidence rather than rerunning it for display or bookkeeping.

Keep secrets, raw history, private paths and deployment identifiers out of public
handoffs. Link the custodian's private inventory where authorized. An external
resource checkpoint is an observation, never a restored lock, grant or admission.
Workspace capacity and project readiness are separate; never add free-space
floors across filesystems. Controller installation, resource-floor enforcement,
identity-safe deletion and NFS durability qualification remain with their owners.

Use the existing scheduler/custodian reports and approved file exchange. Do not
execute a handed-off package until its immutable bytes and applicable proof have
been verified. No recovery note authorizes a deployment, retry, wake or model turn.
