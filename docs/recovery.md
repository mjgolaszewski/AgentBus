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
