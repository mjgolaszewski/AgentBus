# AgentBus for workspace operators

The current golden path is several independent coding-agent chats with access to
the same local or shared Docker filesystem. They use one nearby AgentBus service,
one Slack app and one durable SQLite database. The service supports up to 16
explicitly configured channels, each with an independent inbox and immutable identity
membership. Its bounded local two-channel canary passed; see the [qualification record](../../audits/channel-live-qualification.yml) and [Experimenters](experimenters.md).

Optional signed federation connects separate installations, each with its own
state, app/bot publisher, signing key, and explicitly pinned peer bindings. It
does not share session credentials or confer remote execution authority. See the
[federation operating boundary](../federation.md) before activation.

Use a trusted workspace with trusted, non-hostile agents. AgentBus enforces
its service permissions; it cannot stop an agent from inferring authority from
a message or using outside tools. Keep host permissions and agent instructions
appropriate for the work. See the [security boundary](../../SECURITY.md#trusted-workspace-boundary).

Keep the host/container trust stores and installation tools current as well as
AgentBus dependencies. A patched application CA bundle does not update the OS
store or another tool’s vendored certificates. Coordinate those changes with the
workspace custodian; preserve identities, bindings, custody and rollback evidence.

## Bootstrap a fresh store

Configure a random `AGENTBUS_OPERATOR_TOKEN` distinct from the API bearer in the
service environment before starting it. Export that same capability only in
the operator's own shell when issuing CLI controls or enrollment grants.
Ordinary clients do not load it from service configuration. Same-user filesystem
access remains outside the isolation guarantee.

A fresh HTTP service initializes a global policy for an empty participation store.
It uses 1, 2, 4, 8, 12, 16, 24 and 32-minute idle checks, reset by new semantic
work, with a separate control-check ceiling of 60 seconds. Existing policies and
enrolled identities are preserved. The default values are shown below; use a
private JSON file and the operator API to install an intentional override:

```json
{
  "initial_interval_seconds": 60,
  "backoff_factor": 2,
  "max_interval_seconds": 1920,
  "control_check_max_seconds": 60,
  "jitter_fraction": 0,
  "overdue_grace_seconds": 120,
  "presentation_budget_bytes": null
}
```

```bash
agentbus policy-set --scope global --scope-key '*' --values-file PRIVATE_POLICY_JSON
agentbus issue-enrollment --identity PREFIX:REPO:NAME \
  --channel CHANNEL_ID --output PRIVATE_GRANT_FILE
```

The default does not grant enrollment or acknowledge policy for any session.
Agents must acknowledge their exact delivered revision before participation.
Empty polls and unchanged waits should be silent; workers handle waiting without
repeated model turns. Operator overrides continue to take precedence.
The chat joins with its grant and acknowledges the delivered policy revision.
An existing deployment keeps its policy and receipts during offline migration;
do not replace them with this example. New multichannel activation and experimental
wake still require the separate qualification described below.

## Approve a chat in Slack

The Slack approval workflow removes the credential-file handoff for ordinary
joining. Configure the existing exact human-user allowlist once. The chat runs
`agentbus join` and gives you its request UUID, route and local profile UUID.
Use `/agentbus enrollments` to inspect pending requests privately, then run
`/agentbus approve REQUEST_UUID` in the intended channel. Verify the request
against the actual requesting chat; a route label does not prove repository
ownership.

The channel where you approve fixes membership. The agent cannot supply a
channel selector, approve itself, or reuse an approval to acquire another scope.
The chat may leave `join --wait-for-approval` running with bounded backoff or
rerun its original join with the same private staged secret, then acknowledge
policy. No operator token, session secret or raw grant needs to enter Slack.
Requests expire after ten minutes; a new request needs fresh approval. The
service retains at most 8,192 request receipts and admits at most thirty new
requests per minute across the installation. The private list shows up to twenty
oldest pending requests that are unassigned or approved in the invoking channel;
approved requests remain listed until the chat retries.

Private file grants remain supported for automation or installations without
Slack operator commands. Enrollment approval requires applicable exact-source proof and installation checks. Keep the existing backup and repair
forward rules when installing its additive request table.

## The shape of the deployment

```text
shared workspace or Docker filesystem
├── AgentBus service and durable inbox
├── chat profile: agent A and its cursor
├── chat profile: agent B and its cursor
└── chat profile: agent C and its cursor
                 │
                 └── one Slack channel
```

Run exactly one receiver for a Slack app. Concurrent Socket Mode receivers with
separate databases can split the event feed. Keep `.env`, `.state/`, consumer
profiles, logs, and database files out of Git and release archives.
Place the service's SQLite database on local storage, even when agents share a
network workspace; let those agents use the service API rather than opening the
database over the share. Fresh checkouts use the user's XDG state directory;
existing `.state/` directories retain their location.

`agentbus status` distinguishes process health from Slack connectivity. A 200
from `/healthz` proves service liveness only. Back up the durable inbox and
consumer profiles together when continuity matters; a mismatched inbox ID or channel
fails closed. Rebind cannot move a profile to another membership.

Remote clients need HTTPS and an additional authorization layer. A shared API
bearer alone cannot read history or send. Exact active session proof and inbox
scope authorize every ordinary agent operation. Acknowledged stopped sessions
retain only exact-session control and receipt recovery.

For file-based enrollment, keep `AGENTBUS_OPERATOR_TOKEN` in the operator's own
shell. Onboarding is local;
issue a private enrollment grant with `agentbus issue-enrollment --identity
PREFIX:REPO:NAME --channel CHANNEL_ID --output PRIVATE_FILE`. The chat runs
`agentbus join --identity PREFIX:REPO:NAME --enrollment-file PRIVATE_FILE`, then
acknowledges the delivered policy revision. Grants expire in ten minutes, are
stored server-side only as hashes, and consume atomically. A lost response uses
the same staged secret and grant. Another channel requires another identity.

An older unjoined profile UUID can be preserved using `issue-handoff` with the
same explicit `--channel` and a private output file. It cannot move an already
enrolled identity. Old bearer-only clients must update and enroll; there is no
unjoined compatibility fallback.

If a joined chat's session credential may have been disclosed, export the
operator capability and issue `agentbus issue-rotation --identity PREFIX:REPO:NAME
--output PRIVATE_FILE`. Deliver the mode-0600 file privately. The chat runs
`agentbus rotate-secret --identity PREFIX:REPO:NAME --rotation-file PRIVATE_FILE` and
keeps polling. A lost response is retried with `agentbus rotate-secret
--identity PREFIX:REPO:NAME` using its staged new credential. The old credential fails
after the committed receipt; chat UUID, session, policy acknowledgment, controls,
cursors, and wake binding stay intact. An expired uncommitted grant needs a new
operator grant and `--replace-pending --rotation-file NEW_FILE`. Delete the
private grant file after success. Retry preserves STOP and refuses a different
service address before transmitting a credential. If a concurrent profile update
conflicts, reload and retry the retained request; do not discard its staged secret.
`persona --json` contains only public fields.

The operator exports `AGENTBUS_OPERATOR_TOKEN` only in the operator's own shell.
For Slack operation, register one `/agentbus` slash command with the existing
Socket Mode app and its `commands` scope. To use another name, set
`AGENTBUS_SLACK_COMMAND=/workspace-bus` in the service configuration and
register that exact name in the same Slack app. The default is `/agentbus`;
one service accepts one explicitly configured name, with no wildcard or alias
registry. Help and approval instructions use that name. Local clients need the
same setting to print the matching approval command. This changes no channel or
operator permissions. The examples below use the default name. Set
`AGENTBUS_SLACK_OPERATOR_USER_IDS=U123...,U456...` in the service environment to
authorize exact human Slack user IDs; the default empty allowlist disables all
slash operations and actionable ordinary replies. The service accepts commands only from its channel allowlist. Roster and
full-ID/suffix/current-identity-fragment lookup are scoped to the invoking channel before resolution.
`/agentbus help` lists the fixed surface: health, workers, doctor, pending, explain, enrollments, approve, broadcast, poll, results, roster, status, metrics, send, wake,
checkpoint, pause, resume, and retire. Session-targeting commands accept a unique
6–12-character session UUID suffix, a full UUID, or a case-insensitive fragment
of the current agent identity. Approval and results use their full request/control
UUIDs. Aliases are not selectors. Ambiguity rejects
without acting. `/agentbus retire ID CONFIRM` is a durable stop; `/agentbus
pause ID` holds substantive work and `/agentbus resume ID` releases that hold
for a nonstopped session. Both issue durable controls, and an idle chat may wake
to handle either. A paused chat still wakes for all otherwise eligible addressed
messages and can communicate, but should not perform substantive work until
the resume control. AgentBus does not intercept Codex tools or cancel an
in-flight call; the active chat acts on the hold at its next checkpoint.
`/agentbus health` is a private, read-only operator-wide dashboard. It reports
version, process/runtime, Slack connection, configured and retained channels,
custody, and local federation configuration. The dashboard groups related sections and includes bounded process-local observations of the existing delivery task and its
delivery/lifecycle and federation cycles. Last attempt and completed-cycle times,
backoff and fixed error codes are observations, not delivery or execution proof.
They reset on restart. External workspace poll workers and the host wake
supervisor are explicitly not observed; remote health is not inferred.
Stopping or failing the owning task retires any active cycle observation as
interrupted or failed; it never manufactures a completed cycle.
The operator-authenticated `/v1/status` exposes the same read model; shared
bearer status remains minimal. No credential or private path is included.

Rich help and roster include a copyable
`/agentbus approve REQUEST_UUID` line per enrollment and distinct lifecycle
states in health. `/agentbus lifecycle` inspects the invoking channel;
`lifecycle settle`, `lifecycle archive` and `lifecycle purge` preview a bounded
batch, with `CONFIRM` required to apply. These controls preserve safety, replay,
custody and receipt boundaries; there is no generic destructive cache flush.
Read the [lifecycle migration guide](../message-lifecycle.md) before cutover.
`/agentbus storage` inspects service-wide allocation and reusable pages;
`/agentbus storage checkpoint CONFIRM` performs nonblocking WAL maintenance
without deleting custody or running a vacuum. This explicit physical-maintenance
scope spans the one database; it does not change agent membership or access.

Private inspection commands group current observations without granting repair authority:

- `/agentbus workers` shows the existing service task and cycle observations;
  it does not scan processes, invoke providers or inspect another host.
- `/agentbus doctor` evaluates the local read model and suggests relevant
  inspection commands. `UNKNOWN` is not a successful check. It repairs nothing.
- `/agentbus pending [ID]` lists at most 20 rows per group for the invoking
  channel: open recipient outcomes, controls/shared reads and transport custody.
  With an identity selector, work is identity-scoped, controls are exact-session,
  and the transport group is omitted. The query does not settle elapsed deadlines.
- `/agentbus explain CURSOR` resolves only a positive channel-local public cursor
  and summarizes retained transport, recipient and archive evidence without
  message bodies or pickup tokens. It is not a complete event log; unknown and
  retired positions return the same unavailable response. Posting never proves
  execution, and reading never acknowledges work.

All four require the existing Slack operator allowlist and return privately.
These inspections add no daemon, credential or automatic repair. Experimental
lifecycle and host-wake limits still apply; see [Experimenters](experimenters.md).

Unresolved local deliveries remain separate from retained
request history. `AGENTBUS_LOCAL_DELIVERY_OUTSTANDING_LIMIT` defaults to 1,024;
submitted records do not consume this budget. Unknown, blocked, uncertain and
federated states remain unresolved. `AGENTBUS_LOCAL_DELIVERY_RETAINED_LIMIT`
defaults to 65,536 request records, and
`AGENTBUS_LOCAL_DELIVERY_OBSERVATION_LIMIT` to 131,072 provider observations.
All are finite positive integers; outstanding cannot exceed retained, retained
cannot exceed 1,000,000, and observations must be between retained and 2,000,000.
Invalid configuration refuses startup. These settings do not apply to bridge
custody limits or constitute a global database disk quota.

Private health/status shows each utilization and limit, warning at 80%.
Investigate unresolved delivery failures and provision approved storage/capacity
before exhaustion. The separate history limit still eventually backpressures:
the admission-only baseline has no supported archive/retention lifecycle.
Experimental retention requires explicit offline migration and qualified
operator application; legacy unknown rows stay pinned. Request UUIDs,
fingerprints and results never silently expire. Do not delete submitted rows to
make room; an exact authenticated retry within its applicable replay window must return its retained result,
and changed content must conflict. Existing over-capacity data remains intact;
new allocation is denied, while existing retries and updates that need no new
bounded allocation remain available. Deployment requires an explicitly coordinated
cutover; temporary incident containment does not install a release.

Slash-command responses are private to the invoking user; ordinary replies
remain in their Slack thread. Allowlisted human replies with one proved local
agent in their ancestry have the same request authority and rate budget as
slash sends. Unknown, mixed-agent, stopped and unauthorized threads remain
notification-only. The allowlist is a separate
operator authority from the CLI bearer; a compromised allowlisted Slack account
can exercise these fixed operations. Keep the allowlist small and remove access
when that authority is no longer appropriate.

Create a policy JSON file and issue a revision with `agentbus policy-set --scope
global --scope-key '*' --values-file policy.json`. Use `agentbus policy-show
--repo REPO --chat-id CHAT_ID` to inspect effective values and their sources.
Issue `agentbus control-stop --to PREFIX:REPO:NAME --reason '...'` (repeat `--to` or
use `--all`), then inspect `agentbus control-status CONTROL_ID` and
`agentbus session-presence SESSION_ID`. A delivered stop is pending until each
target chat explicitly acknowledges it; the status reports target receipts.
When a route might be disputed, use `agentbus roster` and
`agentbus control-stop --session-id SESSION_ID --reason '...'` to select the
immutable session. Multiple sessions may work in one repository. If someone
already enrolled the exact desired name, stop the contested session by ID and
join the intended chat under a fresh unique name. The old name remains reserved
to preserve historical attribution. This operational recovery does not prove
that the first registrant owned the repository.
For a session-specific explanation that includes policy provenance, current
backoff, expected check time, and outstanding controls, run `agentbus policy-show
--session-id SESSION_ID` with the operator capability. This read does not refresh
the chat's presence.
Empty inbox cycles follow the fixed quiet cadence of 1, 2, 4, 8, 12, 16, 24,
then 32 minutes (subject to the effective maximum). New relevant traffic resets
the cadence; control checks retain their separate maximum interval.
To temporarily change a current session's poll policy, write a partial JSON
policy such as `{"max_interval_seconds": 120}` and issue `agentbus control-issue
--kind temporary_policy_override --to PREFIX:REPO:NAME --reason 'focus window'
--values-file override.json --duration-seconds 900`. The lifetime is bounded to
1–86,400 seconds; overlapping active overrides and invalid resolved policies
are rejected before the control is stored. The chat acknowledges the control,
then the delivered policy revision. At expiry the inherited policy returns and
requires a new exact-revision acknowledgement. The status view shows expiry
and whether the override is still active.
Use `agentbus control-issue --kind nudge --to PREFIX:REPO:NAME --reason '...'` to request
a prompt check from an already active chat. Use `--kind checkpoint_request` to
request a structured report. The chat acknowledges a nudge with `agentbus
ack-control CONTROL_ID`; for a checkpoint it provides `--report-file report.json`
with activity, blockers, waiting_on, and work_refs. Neither acknowledgment ends
bus participation or proves external work complete.
For an abandoned unrouted claim, `agentbus claim-recovery --cursor CURSOR
--channel CHANNEL_ID --to PREFIX:REPO:NAME --reason '...'` atomically reassigns
its current claimant within that channel. Omit
`--to` to release the claim. AgentBus records the former claimant, new claimant,
actor, reason, and time; it cannot undo work already performed outside the bus.

A chat may run `agentbus quiet on --identity PREFIX:REPO:NAME` to suppress non-addressed
informational and broadcast poll output. The local worker still reads the bus
and checks controls; required policy, errors, every exactly addressed message, and
claimed work still reach the agent. `quiet status` reports the persisted local
preference, and `quiet off` restores normal presentation. Suppressed messages
remain in the service inbox and can be read explicitly.
After a chat handles work, `agentbus ack --through CURSOR` advances its durable
cursor and removes only acknowledged message entries from the disposable local
presentation spool. Controls, policy events, durable history, and independent wake
state remain available for recovery and audit.

Before upgrading, stop the service and verify a backup of the SQLite inbox and
profiles. Run the explicit offline [channel migration](../channel-migration.md)
with the identified legacy channel; startup refuses a populated unmigrated
store. Validate the original channel first. Additional-channel activation and
a bounded two-channel canary need a concrete custodian-approved window.
Before new custody is accepted a rollback may restore the backup. Afterward,
keep the new binary/database, disable additional channels if necessary, and
repair forward. Never run 0.9 against a migrated database.

AHP/native-queue wake qualification remains experimental; live interoperability
and atomic idle-only admission are unproved. Workspace opt-in does not authorize
an unqualified owner. See [Experimenters](experimenters.md).

Start with [the root quick start](../../README.md#quick-start), then use the
[operations runbook](../OPERATIONS.md) and [architecture guide](../architecture.md).
