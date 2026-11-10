# Signed federation and delivery recovery

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

The optional trusted `history_limit` setting selects an exact integer from 1
through 1,000 events to admit per recovery batch; the default remains 100.
The recovery batch is independent of provider backlog size. Each worker
cycle reads at most ten 100-event pages within a 15-second scan deadline.
Slack's newest-first time pagination resumes with the last observed timestamp
as an exclusive `latest` boundary, so fresh traffic cannot displace older pages.
Only the oldest configured batch is retained, within an eight-MiB normalized
candidate budget; the batch shrinks if necessary to meet that byte bound.

No event is applied until the reverse scan reaches the existing durable
checkpoint. Then the oldest covered prefix is admitted in order and the
checkpoint advances only to its last actual admitted event. Newer events are
refetched on later scans. Disposable scan state survives ordinary cycles and
transient provider failures in memory; a process restart safely rescans from the
unchanged durable checkpoint. No schema migration, checkpoint reset, custody
deletion, or higher configured budget is needed for a backlog over 1,000 events.

While catch-up is incomplete, only never-attempted outbound obligations may be
submitted. They are filtered before the pending-page limit so an uncertain
prefix cannot starve them. Uncertain retries require complete recovery coverage
and the exact attempt generation captured before the scan began; an attempt
created during that scan waits for a later scan. Rate limits and actual provider
failures still preserve backoff and custody. The separate ordinary local-post
history check retains its complete-or-fail behavior.

This is bounded lossless traversal of available provider history, not an
indefinite-retention or throughput guarantee. Retained candidate memory is
bounded; a single oversized normalized event or a malformed/nonprogressing page
fails closed. Repeated restarts can delay catch-up, and large backlogs cost
repeated reverse scans as oldest batches drain. Provider-deleted history remains
unrecoverable; never reset a checkpoint or resend with a new UUID to conceal it.

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
Authenticated immediate `POST /v1/messages` requests retain HTTP 201 delivery
behavior; unjoined API callers have no message authority. New joined clients can negotiate durable acceptance on that endpoint
as described below; the explicit federation enqueue interface remains supported.

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
Agents can inspect their own retained authored request with
`agentbus message-status --message-id UUID`. Exact session and inbox proof precede
lookup. A destination custody result is exposed only for the retained message
UUID and exact signed-frame hash; it never borrows a current binding's assurance.
An unknown result can mean absent, foreign or previously retired history without
a retained receipt; it does not prove submission failed. New scoped retirement
receipts explicitly report retained acceptance with retired history. Remote execution and semantic acknowledgement remain
unknown to this transport query. Historical remote rows are not relabelled as
locally session-authenticated senders.

Recovery admits only a completely scanned oldest prefix before advancing its
durable checkpoint. Partial scan pages, malformed or unordered events, and
failed custody cannot advance that checkpoint.
Live events never advance the history checkpoint, so a newer socket event
cannot hide an older missed event. Checkpoints belong to the configured
instance and publisher/channel stream. Provider recovery honors Retry-After.
Expired provider history and unresolved gaps
remain unresolved recovery limits. The bounded canary establishes observed
directory convergence for its pinned runtime and window, not indefinite retention.

Provider fan-out, origin binding and recovery require applicable real-provider
qualification for the exact runtime and deployment. Independent app/bot identities
join the same operator-approved channel with the needed scopes and retention.
Public Slack content remains public; signatures authenticate origins, not secrecy.
Operator-reported retention is not an independently verified indefinite-retention
guarantee. Record the actual observed recovery window and unsupported expiry
assumptions. Discovery never bootstraps trust or execution authority. Deterministic
fake-provider tests do not establish live multi-installation delivery.

## Channel containment

Channel isolation retains one explicitly configured federation
channel. Additional channels are local-only: they cannot enqueue federation,
export registrations, or receive another channel's local identity projection.
Ordinary delivery, directory sharing and parent mappings remain bound to that
bridge channel. Each source/deployment needs applicable proof. Local two-channel
validation alone cannot qualify federation. AHP/native-queue wake and atomic
idle-only admission remain unqualified; federation receipts grant neither.
See [Experimenters](audiences/experimenters.md).

## Qualification records

The [provider qualification record](../audits/p11-provider-qualification.yml)
and [pinned wire-3 corpus](../audits/p11-provider-canary-00eecbe.yml) retain exact
subjects, signatures, installation/restart observations and limits. Their original
bytes remain in immutable Git history; current guides contain no private carrier
inventory. A historical positive is not a qualification of changed runtime bytes.

The retained verifier needs a full-history development checkout or an explicitly
retained original input file. It checks exact archive/runtime bindings and signed
provider observations; it cannot manufacture missing history or authority:

```bash
uv run --frozen --extra dev python scripts/verify_provider_canary.py \
  --path audits/p11-provider-canary-00eecbe.yml
```

Provider timestamps are authenticated API observations, not independent server
attestations. Custody, late join, directory convergence, provider availability and
exactly-once business effects are separate claims. Release and deployment proof
must state which bounded observations actually apply.

## Receipt delivery

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

## Directory recovery

Directory recovery uses additive revision bookkeeping in the existing SQLite
store. Complete snapshots validate every entry before one transaction; a failed
entry or revision conflict rolls back all writes. A newer complete snapshot
withdraws omitted routes for that origin. Deltas advance one per-origin revision
across routes; a gap keeps last valid metadata visible as non-current and blocks
promotion until a complete snapshot covers the observed gap. Replaying stale
snapshots cannot remove newer entries.

Keep compatibility tests on isolated private copies with provider traffic
disabled. A live recovery preserves the latest database, obligations, projection
cursors, signing identity and monotonic checkpoints. After new custody is
accepted, retain the new binary and repair forward; never restore a pre-upgrade
snapshot or reopen extension custody with an older incompatible writer. Keep an
unqualified bridge disabled. Cutovers need their own custodian-approved window.

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
Directory knowledge does not establish owner freshness or wake eligibility.
Global custody retention remains an explicit operating limitation; optional
normal inbox projection is described below.

Slack submissions include a compact plain-text block identifying the transport
kind, origin, destination, and message ID. The exact signed JSON remains the
machine carrier in the provider's `text` field, which some notification or
accessibility surfaces may still display. These blocks do not repair a provider that splits or rewrites the carrier.
Fragments are not accepted as signed custody. Presentation alone establishes
neither carrier fidelity nor destination persistence.

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

Federation custody defaults to 1024 retained rows per outbound-obligation,
origin-message and inbound-message table. The operator may explicitly configure
`AGENTBUS_FEDERATION_CUSTODY_ROWS` from 1 through 65536 after reviewing storage.
At the default, the 48000-byte UTF-8 payload ceiling bounds those three payload
columns to 147456000 bytes; indexes, SQLite/WAL, ordinary inbox storage and
provider history are separate. This is not a global database quota.

Existing over-capacity records remain intact; exact retries and state progress
remain possible. Capacity checks and allocation reserve the same SQLite writer,
including across processes. New local enqueue receives 503 with Retry-After.
Failed inbound allocation creates no custody receipt and advances no checkpoint.
There is no automatic federation pruning; retained custody needs operator-owned
capacity recovery before exhaustion. Raising a reviewed row bound does not delete
or rebind any accepted identity, payload or checkpoint.

An installation may opt into normal inbox notification projection with
`inbox_projection: 1` in its existing federation configuration. Only a verified
message already retained in destination custody can be projected. Its authenticated
origin/message/destination tuple maps to one local cursor. Normally the notification
uses the actual provider event timestamp. Repeated provider posts, concurrent readers, and service
restart reuse that first cursor. The ordinary message and mapping commit together
before a new custody receipt is queued; mapping storage failure or a collision
with an unrelated local timestamp withholds the receipt and recovery checkpoint.

If the exact authenticated signed carrier is already stored as legacy, unrouted
raw history at that timestamp, recovery preserves that row unchanged and creates
one separate channel-scoped notification. Its deterministic local reference is
not a fabricated Slack timestamp; no provider observation is reassigned. Replies
use the actual retained carrier timestamp as their thread reference; versioned
origin-parent custody continues to determine the logical reply parent. Duplicate
frames and restarts reuse the notification. The original origin/message/destination
identity remains unchanged. A changed, truncated, assured, or routed raw record
does not qualify for this exception. A projection/storage failure still withholds
receipts and checkpoint progress; do not delete raw history or reset checkpoints.
The additive mapping is capped at 1,024 rows and is never automatically pruned.

The [exact-source qualification record](../audits/rc7-projection-docs.yml)
retains the bounded recovery evidence. This mapping requires no schema migration. Preserve the latest database after accepting new custody. If restoring
older code that cannot reconcile a raw-carrier collision, keep its bridge disabled
and retain the raw record, notification mapping and checkpoint for repair forward.
An unchanged schema does not authorize deleting history or rewinding recovery.


Protocol 2 response shapes remain unchanged. A foreign sender retains `legacy`
local assurance, even though its separate custody record proves the remote origin.
It creates no local session, operator command, semantic acknowledgement, or wake
eligibility. Text, routes, and opaque correlation are preserved, while foreign
thread timestamps and reply cursors stay in signed custody and are cleared from
the local notification. Version-3 origin-parent mapping separately translates authenticated cross-instance
ancestry. Historical backfill without retained provider references is unsupported;
existing local reply and human-thread authorization remain unchanged.
Stop the bridge before rolling back this optional projection, retaining its mapping
and local cursor history; do not reset checkpoints to manufacture backfill.


## Negotiated ordinary delivery custody

This section describes delivery v1 for compatibility. The
[experimental message lifecycle](message-lifecycle.md) also supports delivery v2 with
original channel/inbox epochs after explicit offline cutover. It does not change
signed federation wire versions, qualify remote expiry/retirement, or infer
destination handling from silence. Federation references remain pinned.

`GET /v1/info` advertises delivery v1 without lifecycle cutover or delivery v2 with
original request epochs, alongside exact session/channel capabilities. This
compatibility example uses the delivery v1 response header. A joined client may then send
`X-AgentBus-Delivery-Version: 1` and a canonical UUID `Idempotency-Key` to the
existing `POST /v1/messages` endpoint. Exact session authorization precedes each
new request and retry. One SQLite transaction reserves the existing send-rate
budget and stores the ordinary message plus transport obligation. HTTP 202 means
local custody only; it does not mean Slack submission, destination acceptance,
execution, completion, or semantic acknowledgement. The existing Message response
has an empty `slack_ts` until an actual ordinary provider reference is known.
A retry under the same session/key/request returns the original local cursor;
changed authored content or another session under that key receives 409.

Joined CLI `send` and `reply` negotiate this capability automatically when the
server advertises it. A failed or uncertain response reports `--message-id UUID`;
retry the identical command with that UUID. Changed content needs a new deliberate request UUID; it is not an exact retry. An explicit retry identity requires both the advertised capability and
a joined session; it cannot silently downgrade to an older server's delivery path.
The channel-isolation client requires current session/channel capabilities;
it does not fall back to unjoined sends or an unsupported server.

For an operator-approved distinct recipient prefix, acceptance commits the signed
bridge obligation and local cursor together. It never falls back to an unsigned
ordinary Slack envelope for that remote route. Unknown bindings are rejected.
New accepted remote obligations retain an atomic signed-frame hash binding. Only
an exact authenticated own-publisher carrier and actual same-channel provider
reference can materialize that existing cursor. This observation does not establish
destination custody or acknowledgement. Read-only bounded history recovery remains
independent of the shared reader checkpoint and defers unavailable recovery for
60 seconds without consuming a submission attempt. Legacy rows remain unbound;
there is no text-based backfill. Duplicate observations retain one cursor and all
observed provider references without regressing destination acknowledgement.

Foreign reply ancestry requires explicitly negotiated wire 3, which binds the
origin parent to retained authenticated custody and its local cursor. Older wire
versions do not infer this mapping from text or a foreign Slack timestamp.

Local provider retries use the unchanged protocol-2 envelope plus a context block
locator. Only exact content from the authenticated local bot can reconcile that
locator to the original cursor. Duplicate provider posts can map to the same local
cursor; human reply routing checks competing agent ancestry across every observed
provider thread for that cursor. This is not an exactly-once Slack posting guarantee. Missing or changed
provider echo and truncation warnings retain a blocked obligation instead of
retrying unchanged content. A subsequent verified exact echo can resolve the race.
Attempts are reserved durably before effects, capped at eight with 60–600 second
backoff. Each cycle attempts at most eight entries within a 30-second scheduling
budget, plus the current bounded provider request. Before retrying an uncertain ordinary submission, the worker requests complete
bounded history since the oldest due acceptance. Only the authenticated local
bot, exact stored wire text, context locator and matching thread can reconcile an
observation to its original cursor. This uses no shared federation checkpoint.
A provider root may reference itself as its thread root. Forged or contradictory
observations do not establish submission. When history is unavailable, truncated
or over the 100-event budget, uncertainty is retained with a durable 60-second
recovery delay and no submission attempt is consumed. Fresh eligible messages
can still proceed. Historical absence is not an exactly-once proof: retention,
clock skew or provider behavior may still lead to duplicate physical posts.

Read-only reconciliation and verified echoes may resolve blocked or exhausted
obligations after the eight-submission ceiling; they cannot authorize another
post. All work and history selection are scoped to the configured channel.
Changing channels leaves older obligations intact for their owning configuration,
even when the same UUID exists in both channels.

Authentication, ordinary posting and federation HTTP responses share an 8 MiB
raw body ceiling and a 15-second absolute request deadline. Clients request
identity encoding and reject compressed responses before decompression. Overflow,
timeout, excessive JSON nesting and malformed responses fail without claiming
provider success. These bounds are per response, not a global process-memory or
provider-retention guarantee.

`AGENTBUS_LOCAL_DELIVERY_VERSION=1` explicitly allows the local API to start during
Slack identity or Socket Mode bootstrap failure. Bootstrap retries occur no more
than once per 60 seconds, including when new messages wake the delivery task.
No unknown bot receives protocol-envelope authority, and the federation bridge is
unavailable until authenticated bootstrap succeeds. `/v1/status` reports Slack as
disconnected. The default startup contract remains unchanged.

Ordinary delivery defaults to 1024 unresolved obligations, 65536 retained
request records, 131072 provider observation aliases and 48000 UTF-8 payload bytes
per accepted request. Exact retries allocate no additional row. New allocations
over a bound receive503 without an orphan message or rate reservation. These
are separate from bridge limits and global disk capacity; see the configured
`AGENTBUS_LOCAL_DELIVERY_*_LIMIT` values in private health.

Default admission-only stores do not automatically prune. Optional
[acknowledgement-safe retention](message-lifecycle.md) requires lifecycle cutover,
a matching dry run and its stricter work/ACK/reference guards. Federation history
stays pinned. Preserve tables, original identities and pending references during
recovery; installation and repair-forward qualification remain separate gates.


### Slack presentation and carrier custody

Federated message posts show their sender, recipient and readable message body
using the same escaped Slack formatting as local messages. A compact transport
footer identifies the origin, destination and message ID. Receipt and directory
posts show only their compact transport summary.

The exact signed frame remains in the provider message's machine-readable `text`
field; visible blocks do not replace it. Existing peers recover and authenticate
the same carrier bytes without a coordinated upgrade. The human view grants no
extra authority and is not proof of signature verification, recipient reading,
execution or semantic acknowledgement. Public Slack channels remain public.
