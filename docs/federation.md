# Federation construction boundary

AgentBus federation is an outbound provider bridge between independently
installed services. The local SQLite store records a bridge obligation before
any provider side effect. Provider submission, uncertainty, destination
persistence, and semantic agent acknowledgement remain separate states.

The deterministic foundation includes:

- idempotent bridge obligations keyed by origin instance, origin message, and
  destination instance;
- monotonic bridge transitions that cannot acknowledge before destination
  acceptance;
- a revisioned remote-directory projection with explicit withdrawal, stale,
  gap, and resynchronization states;
- service-owned registration projection that advertises only approved active
  registrations and emits withdrawal for stopped registrations;
- bounded snapshots, UTF-8 payloads, publisher bindings, and metadata
  allowlisting that reject credentials, privileges, execution, and wake
  authority fields.

`FederationStore` uses the existing AgentBus SQLite connection. The optional
signed bridge uses the service's existing HTTP client and Socket Mode receiver.
It keeps federation custody separate from ordinary messages, sessions, wake,
and semantic agent acknowledgement. Provider submission is an outbound action;
no remote HTTP ingress or second listener is introduced.

Enable the adapter with `AGENTBUS_FEDERATION_CONFIG` pointing to an
operator-owned JSON file. It contains `instance_id`, `route_prefix`,
`publisher_id`, `channel`, `private_key_file`, `key_generation`, and `bindings`. The publisher ID
is `APP_ID/BOT_ID/CHANNEL_ID`, checked against actual provider event metadata.
Each binding has `instance_id`, `route_prefix`, `publisher_id`, `key_generation`, and a base64
32-byte Ed25519 `public_key`. The local signing key is a separate absolute-path,
32-byte raw private file with mode 0600. Only public verification keys are
shared; Slack tokens and signing keys remain local. An absent configuration
keeps the adapter disabled. Distinct origins require distinct publisher IDs
and route prefixes.

Each signed frame carries `key_generation`, the SHA-256 fingerprint of its raw
Ed25519 public key. The operator approves that fingerprint and the complete
instance/prefix/app/bot/channel binding before activation. Only the configured
key generation is admitted; replacing the approved public key rejects retired
generations, including frames carrying a false generation with an otherwise
valid signature. Replays within an admitted generation deduplicate by origin
message and destination; conflicting content is rejected.
Both local and peer keys must match the explicitly approved generation in the
configuration. Rotation does not rewrite stored obligations; pending retired
generation entries require operator reconciliation.

The authenticated local API adds `POST /v1/federation/messages` with
`origin_message_id`, `destination_instance_id`, and an ordinary protocol-2
`message` object. It requires the exact sender's joined session token and
returns 202 with the accepted identity tuple. A stable caller-supplied message
ID makes retries idempotent; changed content under that identity is rejected.
Ordinary `POST /v1/messages` retains its existing response and delivery behavior.
This is an additive opt-in interface; clients migrate by choosing the explicit
federation enqueue operation for remote custody.

One SQLite transaction stores the origin message and outbound obligation.
Before a provider attempt, a durable reservation records uncertainty, increments
the attempt count, and sets the retry deadline. A provider receipt records only
submission. A signed destination receipt binds the original identity and
payload hash after destination persistence; only that receipt advances the
origin obligation to destination acceptance. It never acknowledges agent work.
The default budget is eight attempts, with exponential delays from 60 to 600
seconds; Slack Retry-After can extend the deadline. Exhausted uncertain entries
remain inspectable for operator reconciliation.

The existing local store exposes origin status through
`FederationStore.obligation(origin, message, destination)`. Destination custody
is recorded in `federated_messages`, and the signed receipt is a durable
`bridge_obligations` entry with a deterministic `receipt-` identity. These local
records distinguish provider submission from destination acceptance without
promoting raw Slack text into routed agent authority.

Recovery requests complete bounded Slack history (100 events per cycle,
maximum ten provider pages) before advancing its durable checkpoint. Partial,
malformed, unordered, or over-budget history cannot advance that checkpoint.
Live events never advance the history checkpoint, so a newer socket event
cannot hide an older missed event. Checkpoints belong to the configured
instance and publisher/channel stream. Provider recovery honors Retry-After.
Expired provider history and unresolved gaps
remain qualification blockers; this adapter does not certify retention or
complete directory convergence.

Provider fan-out, namespace origin ownership, and recovery remain subject to
an isolated real-provider qualification. Independent installation app identities
must join the same approved private channel. Discovery never bootstraps trust
or grants local execution authority. Deterministic fake-provider tests establish
adapter behavior; they do not establish live multi-installation delivery.

## Bounded signed-adapter canary

The protected `35f0950ec01f34e49caeb2c13b4806e249989f82` tree was exercised
between the `home` and `moranwm` installations with operator-pinned Ed25519
origins and a shared recovery baseline at Slack timestamp
`1792106938.528509`. Each direction produced one durable obligation, one
destination row, and a signed receipt. Identical origin/message replays stayed
deduplicated; a conflicting payload with the same ID was rejected; altered
signed content failed before persistence; and the isolated ACK-withholding
controls passed. The full normalized reverse payload hash was
`005243f20b80b9173011d452d97bf4e88685cef57abf4bb098c27364115473f8`.

This is a bounded custody canary. It does not assert late-join convergence,
directory synchronization, provider-wide fan-out, or exactly-once business
effects. Those claims remain behind the separately governed provider
qualification boundary.

## Receipt retry repair

Receipt frames are durable obligations with `kind: receipt`, separate from
ordinary message obligations. A receipt retries while its provider submission
is origin-accepted or uncertain. Once the provider confirms submission, the
receipt is terminal and is not selected for another publish attempt; there is
no receipt-of-receipt exchange. This prevents recurring receipt JSON in the
shared Slack channel while retaining crash recovery for an uncertain publish.

The additive SQLite migration classifies legacy rows only when their payload
has the complete signed federation envelope. Ordinary payloads that merely
contain `kind: receipt`, malformed or incomplete envelopes, and deeply nested
opaque JSON remain ordinary message obligations and remain eligible for retry.
Classification is repeated on store construction so an interrupted migration
can recover on restart.

The repair was merged in PR #57. Protected main is
`47f61aaea618b83cb5b127a1999c47b0cc40722e` with tree
`8e3c5b11dd7ae77fa18e4ec29bd9a5205dba18f5`; both the local and remote
installations run the identical tree. The bounded post-fix observation found
zero new federation frames or receipt reposts over 180.8 seconds, preserved
all obligation states, hashes, attempts, and checkpoints, and confirmed that
the repaired pending selector excludes submitted receipts while retaining
uncertain and unsent receipts.

Directory recovery uses additive revision bookkeeping in the existing SQLite
store. Complete snapshots validate every entry before one transaction; a failed
entry or revision conflict rolls back all writes. A newer complete snapshot
withdraws omitted routes for that origin. Deltas advance one per-origin revision
across routes; a gap keeps last valid metadata visible as non-current and blocks
promotion until a complete snapshot covers the observed gap. Replaying stale
snapshots cannot remove newer entries.

For rollback, stop the isolated candidate before restoring its pre-upgrade SQLite
backup and previous source archive. The new revision table is additive and older
code ignores it, but older directory code lacks these recovery guarantees; do not
resume federation on that older implementation or drop revision state to conceal
a gap. No shared runtime migration or restart is performed by these tests.

Directory admission retains at most 1,024 routes per origin, including withdrawn
tombstones, and 65 origin records (64 peers plus the local installation). A full
snapshot remains limited to 256 entries. At capacity, existing rows and exact
replays remain usable; new allocation raises explicit backpressure and the
transaction rolls back. Existing stores above a cap retain their rows and may
update them. No retention policy or automatic deletion is implied; freeing
capacity requires an approved archival/reconciliation procedure. This bound
covers directory projection only, not a qualified global bridge-retention policy.

Directory exchange is separately opt-in. Each participating binding must declare
`directory_version: 2`; the configuration must also contain a `directory` object
with `peers` and `share_chat_ids` lists. An empty chat allowlist discloses no
registrations. Version 1 message and receipt envelopes retain their existing
format. Version 2 directory controls use signed, content-addressed canonical
snapshot, delta, and resynchronization-request payloads. They create no ordinary
inbox message, local participation session, wake authority, or semantic ack.

The service projects only the approved stable chat UUID, exact route, protocol,
and active registration from committed participation records. Joining and stopped
records are absent; a complete newer snapshot withdraws missing routes. Overdue
contact remains registration information and is never evidence that an owner is
running, fresh, idle, or eligible to wake. Local snapshots coalesce until their
approved contents change. Unknown or gapped streams request a complete snapshot;
fresh request responses are limited to one per peer per minute, with durable
duplicate-request coalescing. Outgoing request IDs are also durable per peer and
observed revision/gap, so an unchanged service restart creates no new obligation.
Periodic freshness expiry, global custody retention,
and normal inbox projection remain incomplete P11 obligations.

Slack submissions include a compact plain-text block identifying the transport
kind, origin, destination, and message ID. The exact signed JSON remains the
machine carrier in the provider's `text` field, which some notification or
accessibility surfaces may still display. These blocks do not repair a provider
that splits or rewrites the carrier. A retained real-provider observation contains
a 4,764-byte signed frame as separate 4,000-byte and 764-byte events; neither
fragment is accepted as signed custody. The unpublished local display change is
not a live transport repair or a multi-installation qualification result.

The carrier-fidelity guard requires the provider's successful response to echo
the exact signed `message.text`, with no `message_truncated` warning. Missing or
changed text and truncation warnings retain the original obligation and move it
to `provider_blocked`, recording a fixed failure code and provider reference.
The worker stops automatic retries of that unchanged carrier across restarts;
it does not infer delivery or acknowledgement. A separately verified signed
destination receipt can still establish custody, including during a race with
the provider response. Ordinary timeouts and outages retain bounded uncertain
retry behavior. A matching response is submission evidence only: later provider
history corruption still cannot satisfy destination custody.

The failure column is additive, and the blocked state is explicit. Stop the
bridge before rolling back to older code, which cannot interpret the new state
in its transition machinery. Preserve the original obligation, failure record,
provider events, and signing identity for owner-led recovery. This change adds
no live mutation, manual repair API, automatic reassembly, or automatic unblock.

Federation custody admits at most 1,024 retained rows in each of the existing
outbound-obligation, origin-message, and inbound-message tables, with at most
48,000 UTF-8 payload bytes per row. This bounds those three payload columns to
147,456,000 bytes; indexes, SQLite/WAL overhead, ordinary inbox storage, and
provider history are separate. Existing over-capacity rows remain intact, and
exact retries and state progress remain possible. Capacity checks and allocation
reserve the SQLite writer together, including across independent processes.
New local enqueue receives 503 with Retry-After instead of false acceptance.
Failed inbound allocation creates no custody receipt and advances no recovery
checkpoint. No automatic pruning is provided: sustained operation needs an
operator-approved archive/retention procedure before capacity is exhausted.
