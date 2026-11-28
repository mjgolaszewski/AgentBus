# Federation custody maintenance

Federation has both pending work and retained custody. Successful delivery drains
pending submission work, but identities, hashes and receipts remain necessary for
replay detection and lost-response recovery. Capacity counts retained rows.

## Safe compaction

Only outbound message obligations with an authenticated `acknowledged` state,
last changed at least fourteen days ago, and no retained local-delivery reference
qualify. Destination acceptance, inactivity, expiry and operator assertion do not
supply acknowledgement. Existing ordinary federation does not provide a general
semantic-ACK producer: those accepted legacy records remain pinned. Inbound
custody and projections also remain pinned pending a qualified replay/consumer
retirement contract. This feature cannot clear every full table.

Eligible rows move transactionally into a compressed, digest-verified replay
archive. Original identities and outcomes remain inspectable; an identical late
retry returns the retained terminal result without posting or allocating new
active custody. Conflicting content fails closed. Archive corruption requires
recovery rather than treating the original request as absent.

## Automatic operation

A custodian can configure `AGENTBUS_FEDERATION_AUTO_COMPACT=1` at startup. The
default is disabled. Once hourly, the existing delivery worker scans at most
1,000 rows and compacts at most 100 eligible rows. A durable rotating checkpoint
prevents pinned history from starving later records. No new daemon, network
probe, provider call or acknowledgement is introduced. Restart preserves the
last observation and scan position. Private operator diagnostics expose whether
compaction is enabled, its last sample, compacted count and sampled pin reasons;
these are bounded observations, not global eligibility totals.

The replay archive is capped at 65,536 records and 512 MiB of compressed payload,
with bounded per-record metadata. Exhaustion blocks compaction without deleting
original custody. Tombstones do not expire automatically. Compaction frees active
row capacity; it does not promise to shrink the physical SQLite file or remove
all retained payload bytes.

## Offline preview and apply

The custodian must stop the service and preserve a verified database backup.
Run from the installed source using its supported interpreter:

```sh
python scripts/federation_retention.py --database /protected/bus.sqlite3 --plan /protected/retirement.json
python scripts/federation_retention.py --database /protected/bus.sqlite3 --plan /protected/retirement.json --apply
```

Preview is read-only and creates a new private plan file. Apply requires the
exact preview digest, database file identity and inbox scope, then revalidates
all selected rows under one writer transaction. A changed preview requires fresh
inspection. Any storage, hash or quota failure rolls back the entire batch.
The command takes the same exclusive database lease as the service and refuses
concurrent service ownership. It has no provider or credential access.

After any compaction, retain the current binary and database and repair forward;
do not open the database with an older binary that cannot consult replay
archives. Before accepting new custody, rollback may restore the verified backup.
An uncertain apply outcome requires inspection of the original archive and
active rows, never a fabricated acknowledgement or blind fresh-request retry.

## Early capacity notices

`AGENTBUS_FEDERATION_CAPACITY_NOTICES=1` separately enables best-effort private
notices to the existing allowlisted human operators, at most sixteen. It uses
the configured federation channel and existing Slack connection credentials.
Local counts are sampled at most once per minute. Each table emits at most one
notice per 80%, 95% and 100% threshold in a pressure episode; recovery below 70%
resets the episode and permits one recovery notice. State survives restart.

A notice reserves its durable identity before using one slot in the existing
shared posting budget. An ambiguous response is retained without blind retry.
Private submission is not proof of human reading. Notices are disabled by
default, make no extra periodic network calls when disabled, and grant no
retention, retry, resource or execution authority. Existing health warnings
remain available independently of notice delivery.
