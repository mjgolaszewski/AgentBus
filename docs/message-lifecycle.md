# Experimental message lifecycle

This guide describes **experimental message lifecycle behavior** in the current
release. Protected-source, archive/install, populated-store and bounded live
checks apply only to the exact subjects in the
[release records](https://github.com/mjgolaszewski/AgentBus/releases).
Each deployment still requires its own stopped, backed-up migration and client drain.
The [versioned contract](../contracts/messages/v1/message-lifecycle.contract.yml)
defines the behavior. Protocol-2 Slack envelopes remain unchanged.

## Read the states separately

| Layer | States | Meaning |
| --- | --- | --- |
| Transport | pending, submitting, uncertain, blocked, submitted | Provider submission custody; none establishes handling |
| Expired transport | expired unsubmitted, expired uncertain | No first post after recipient expiry; an already uncertain attempt remains unresolved |
| Recipient | unclaimed, picked up, handled | Explicit session-owned pickup and handling receipts |
| Recipient deadline | expired unclaimed, recovery required | Unclaimed work returned as a queryable outcome; previously picked-up work needs recovery, not blind resend |
| Shared broadcast/poll | unread, read acknowledged, expired unread | One shared payload, fixed target membership and independent read/answer outcomes |
| Retention | hot, archived, purged, legacy unknown/pinned | Payload custody and retention; none creates an ACK |

Polling, presentation cleanup, Slack posting, wake queue acceptance and host
completion do not themselves acknowledge a message. Returning work does not
create a recursive bounce message. The sender inspects `agentbus message-status
--identity ID --cursor CURSOR`; recipients see their own outcome, while the
sender sees aggregate target states without private pickup tokens.

## Client behavior

Delivery v2 requires a service-issued channel/inbox epoch. The client privately
stages its original payload, fingerprint, scope, UUID and epoch before sending.
An uncertain response never permits changing that UUID or replacing its epoch.
An explicit `--message-id` recovers an existing private stage; without its original
stage and epoch, the client refuses before posting. Let the CLI stage a new UUID
for new work rather than inventing a retry identity.
Known accepted requests can replay through their declared replay window even
after their admission epoch closes. New requests using a closed epoch fail;
expired requests return HTTP 410 and must not be recreated automatically.

Use `agentbus pickup --identity ID --cursor CURSOR` when starting addressed
work. The default receipt is stable for that session and cursor. After actually
handling it, use `agentbus complete --identity ID --cursor CURSOR`, or the
existing `agentbus ack --identity ID --through CURSOR`. ACK commits bounded
server handling receipts before advancing the local cursor or retiring the
presentation. Its result distinguishes handled, already handled, expired and
legacy-unknown outcomes. Large ACK ranges can return a partial cursor and
`more: true`; repeat the same requested range to continue.

A stopped session cannot pick up new work. Its exact credential and original
receipt can still complete work it already owned. A custom pickup receipt must
be supplied again using `--receipt-id UUID`. Rotation, channel mismatch and
another session's receipt fail closed.

An elapsed work lease is notification-only for wake processing, including when
the prior host submission had an uncertain outcome. Owning its completion
receipt does not authorize another turn. Preserve the original attempt for
explicit owner recovery; no automatic lease renewal or resubmission occurs.

Legacy history receives no retroactive handling or deadline claim. An explicit
local ACK may retire its presentation, while its server classification remains
unknown and pinned. To recover a known pre-cutover send, supply the original
`--message-id UUID --legacy-replay` and identical content. Unknown legacy UUIDs
cannot allocate new custody; this flag is not a compatibility fallback.

Wake candidates are revalidated against the current scoped service outcome.
Expired/handled projections cannot submit work; unknown outcomes preserve
evidence and remain notification-only. The candidate host-submission path records pickup
before submitting a turn. This does not qualify AHP/native queue interoperability
or atomic idle-only admission; owner/backend and binding requirements still apply.

## Operator controls

Allowlisted Slack operators get private, channel-scoped controls:

```text
/agentbus lifecycle
/agentbus lifecycle settle
/agentbus lifecycle archive
/agentbus lifecycle purge
```

The last three commands preview effects. Append `CONFIRM` to apply a bounded
batch with a durable operation receipt. Settle distinguishes unclaimed expiry,
running-work recovery and shared unread expiry. Outcome queries are read-only:
`deadline_elapsed` applies to due unclaimed work and stays true after settlement
materializes `expired_unclaimed`. `lease_elapsed` applies to due picked-up work
and stays true in `recovery_required`. They describe the applicable lifecycle
condition, not a generic comparison against every historical timestamp: a picked-up
message does not expire against its old pickup deadline, and handled work has
neither pending condition. Reading does not settle, renew a lease or acknowledge work.
An expired reply parent stays rejected. For a genuinely new status report, create
a new channel-local broadcast; do not replay the expired request under a new UUID.
Continuing expired actionable work needs a fresh operator request. Archive publishes a bounded,
compressed, digest-verified payload in the same SQLite database. Purge removes
only eligible closed history after replay and retention windows, while checking
claims, ancestry, custody, provider references and federation pins. Unknown
schema references pin data. Cursor high-water marks and ingress retirement
floors prevent reuse or recreation of retired history.
Scoped history pages explicitly flag retired gaps. A known retired position
returns HTTP 410 rather than looking like newly available work; its public
position is never allocated again.

Purge also waits until the channel retirement floor covers the original provider
reference and every observed numeric alias. A newly observed alias can make a
preview ineligible; application rechecks under the owning writer transaction.
Provider-age predicates run before the bounded batch selection, so a prefix
pinned by recent aliases cannot hide later eligible rows. Timestamp comparison
keeps exact decimal precision rather than rounding through floating point.
A late Socket event carrying a delivery locator with no retained accepted
request cannot create fresh custody. Its transport acknowledgement means only
that the event was ignored, not that an agent received or handled work. Ordinary
human messages without a delivery locator keep their existing routing rules.

External JSON wake intents are not database foreign keys. Running or uncertain
recipient work stays pinned. If terminal history referenced by a delayed local
intent is retired, HTTP 410 leaves the intent, uncertain attempt, spool and ACK
unchanged and reports notification-only; retirement never authorizes a new turn.

There is no generic cache flush: authoritative retry guards, custody, receipts,
credentials, bindings and wake intents are not caches. These controls do not
delete arbitrary files, restart services or infer remote delivery/expiry.
`/agentbus health` separates transport, recipient, shared-read and retention
counts, reports storage headroom, and warns at 80% utilization.

`/agentbus storage` reports the physical and reusable footprint. The explicit
`/agentbus storage checkpoint CONFIRM` operation performs nonblocking,
service-wide WAL maintenance across the one database. It does not delete message
or receipt rows, run a vacuum, acknowledge work or change channel membership.
An active reader/writer can defer maintenance without losing custody.

Operator archive commands have exactly 32 MiB of separate maintenance headroom
above the ordinary message budget. New sends cannot use it. Preview and retained
operation results expose the pre-operation physical/reusable/WAL footprint and
archive admission ceiling. Each transaction reserves its entire bounded batch
of at most 100 payloads before changing custody. This permits cleanup when
ordinary admission is exhausted; it is not an OS allocation guarantee.

If a reader pins WAL, custody remains intact and new admission stays blocked.
Release that reader through its owner, run the nonblocking checkpoint, then
repeat archive/purge batches as eligible. At the maintenance ceiling, archive
fails without partial retirement. There is no unlimited fallback: inspect pinned
obligations and arrange operator capacity recovery before retrying. No implicit
vacuum or deletion occurs.

## Offline cutover

The custodian-reviewed ordinary-request defaults are a 24-hour pickup/work lease and admission epoch,
seven-day replay/archive windows, 30-day payload retention and a 1 GiB
message/archive admission budget. Final-source qualification and live
configuration remain separate. Occupied database pages,
WAL/SHM and in-database archives are accounted together; freed pages are reusable,
and physical allocation is reported separately. Nonblocking WAL maintenance runs
under admission pressure without deleting custody or vacuuming. This is not an OS disk
quota, nor a budget for external backups, other repositories or client stages.
Safety and receipt recovery remain available under message admission pressure.

Drain and restart long-lived poll/wake clients with their exact bindings and
enabled preferences preserved; replacing CLI files does not reload a process.
Stop the service, then run the verified candidate interpreter from its checkout:

```bash
python -m src.agentbus_lifecycle_migration_service --db-path /state/messages.sqlite3
python -m src.agentbus_lifecycle_migration_service --db-path /state/messages.sqlite3 \
  --apply --backup /backups/messages-before-lifecycle.sqlite3
```

Apply requires a new backup file and the stopped-service lease. The migration
preserves existing data, marks old pickup state unknown, freezes unknown legacy
allocation and installs old-writer guards. It reclaims no legacy storage merely
by running. Start the candidate in local custody mode
(`AGENTBUS_LOCAL_DELIVERY_VERSION=1`; HTTP negotiates delivery v2). Before new
writes, rollback can restore the verified backup. After accepting new custody,
retain the new binary/database and repair forward; never run an older writer
against the migrated database.

Legacy unknown and federated references stay pinned. Shared opaque Slack retry
keys and control payloads also remain pinned under independent finite limits;
their retirement is not qualified. Archive/purge require explicit operator
application; no automatic history reclamation is claimed. Retained administrative
receipts, safety controls and external/client storage still require capacity
planning. See [Experimenters](audiences/experimenters.md) for qualification rules.
