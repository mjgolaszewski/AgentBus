# Changelog

## [0.9.1rc5] - 2026-10-19

- Prevent dependency-blocked replies from starving eligible local delivery; settle expiry before provider selection while retaining uncertain custody.
- Defer ordinary wake when fresh mandatory controls or policy have not reached the presentation projection.
- Preserve service-authorized alias messages in quiet mode after rename.
- Check every module import and API handler, execute query nonmutation probes, and correct enrollment ownership without changing authority.
- Disclose Slack operator allowlist before enrollment and align release, human-reply authorization and Linux-native guidance.
- No schema migration, new transport, live AHP/atomic-wake qualification, publication or deployment is implied.


All notable changes to AgentBus are documented here. AgentBus follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

- Correct the editorial audit summary to count all 93 reviewed product documents; retain historical proof and unfinished qualification states.

- Record exact RC5 candidate proof, reproducible archive and SDK-free installation; complete the five corrective work orders while keeping publication, deployment and P12 owner-wake qualification separate.

- Record operator acceptance of RC4 grouped private health and completion of the requested safe cleanup; close the authored RC4 diagnostics workitem while keeping owner-wake qualification open.

- Record RC4 publication, exact-subject certification, workspace upgrade and verified safe cleanup; preserve experimental owner-wake limits and record completed human UI acceptance.

- Record the immutable RC3 release, exact-subject proof, isolated installation and bounded lifecycle canary, retaining the initial probe failure and unqualified owner-wake limits.

- Preserve bounded retention progress when recent provider references pin earlier rows; compare provider timestamps at exact decimal precision before selecting each purge batch.

## [0.9.1rc4] - 2026-10-18

Published as immutable `v0.9.1rc4` after protected PR and exact-main proof, archive installation, and a bounded two-channel canary. The workspace upgrade is complete; the operator confirmed that grouped health and Workers are readable and private.

- Group health and roster output with spaced sections and textual states instead of repeated colored dots; preserve numeric code styling and escaped Slack rich text.
- Observe the existing service delivery task and delivery/federation cycle attempts, completion, backoff and sanitized failures in bounded process-local memory. External poll/wake workers and remote health remain explicitly unobserved.
- Add private operator-only `workers`, `doctor`, `pending [ID]` and `explain CURSOR` inspection commands. Channel-local retained-state reads never settle deadlines, acknowledge work, retry submissions or change custody.
- No schema migration, monitoring daemon, provider probe, new dependency, host-wake qualification or automatic repair is introduced.

## [0.9.1rc3] - 2026-10-18

Candidate notes; publication, final custody and live qualification remain pending.

- Construct an experimental ordinary-message lifecycle with versioned replay epochs, private retry staging, exact-session pickup/handling, unclaimed expiry and recovery-required running work; release/live qualification remains pending.
- Add bounded, digest-verified SQLite archive and reference-safe retirement, preserving public high-water marks and closed request identities; delay purge until all provider references are covered and reject unknown retired delivery locators without new custody; legacy unknown and federated custody stays pinned.
- Add private lifecycle/storage controls, distinct transport/recipient/shared-read/retention health states, richer help/roster and copyable enrollment approval commands. Shared opaque control history retains a disclosed finite-capacity limit; no generic destructive cache flush or automatic purge is introduced.
- Separate outstanding local delivery admission from retained idempotency history, with finite operator-configured request/observation limits and private utilization warnings; retain retry identities and disclose which legacy/shared histories remain pinned.

## [0.9.1rc2] - 2026-10-17

- Add file-free enrollment requests approved privately by allowlisted Slack operators in the intended channel; retain canonical grant custody, staged-secret recovery, immutable membership and file-grant compatibility.

- Add channel-scoped operator broadcasts and polls using one shared durable payload with per-session read receipts or answers, plus private results.
- Mark sender with an outbox icon and recipient with an inbox icon; improve mobile health readability with colored state symbols and theme-styled numeric values while preserving protocol carriers and delivery locators.

- Route ordinary Slack thread replies from allowlisted human operators with the same request authority and persistent rate budget as slash sends, including human-rooted request threads; ambiguous, foreign, stopped and unproved targets remain notification-only.
- Resolve slash-command targets through unique current identity fragments within the invoking channel, with ambiguity denied before effects.
- Add a private operator-wide health dashboard through `/agentbus health` and operator-authenticated status; ordinary agent status stays minimal.
- Use escaped Block Kit rich text while preserving protocol envelopes, signed carriers and durable delivery locators.
- RC2 live qualification requires a fresh bounded two-channel canary; AHP/native-queue interoperability and atomic idle-only wake remain unqualified.

- Keep historical live-proof rejection strict for release candidates; update its diagnostic assertion for unsupported prerelease metadata without widening the verifier.

## [0.9.1rc1] - 2026-10-17

Release candidate for channel-isolated operation. Requires updated clients and
an offline, backed-up migration from 0.9.0; do not reopen a migrated database
with the old binary. Additional channels are local-only.

- Serve up to 16 explicitly configured channels through one service, database, Slack app and socket. Each identity has one immutable channel, assigned by an operator-issued enrollment grant.
- Require exact-session authorization for reads and writes, with channel-local public cursors, scoped replies/claims/controls, client projections and fail-closed wake bindings.
- Qualify a bounded local two-channel canary, including populated-store continuity, duplicate requests, foreign-route/inbox rejection, provider observation and one managed restart. Four isolated test sessions were stopped afterward. Earlier test-oracle failures remain disclosed.
- Review product documentation with per-file line coverage, current contract models, privacy-safe examples and an Experimenters guide.
- Live AHP/native-queue interoperability and atomic idle-only admission remain unqualified; Issue #75 stays open. No new federation qualification or deployment is implied.

- Use one configuration preflight for serve, start, and enabled autostart; accept either channel form and reject simultaneous forms before process execution.

- Stack channel-isolation construction in P12 with versioned authorization contracts and cross-channel negative controls; bounded local two-channel qualification now passes on the protected construction subject.

- Strengthen offline wake qualification requirements for exact backend identity, reconnect generations, and operation-owned recovery after uncertain submission; classify October 17 upstream reports without enabling live wake.

- Include owner-wake qualification cases in the canonical test gate and expand offline schema controls for identity and message-origin boundaries.

- Begin #75 owner-wake qualification with pinned source and local capability inventory, explicit admission requirements, and deterministic adapter boundary controls. Added an offline probe of the pinned upstream AHP reducer, with source-integrity and semantic negative controls. No runtime behavior changes.

## [0.9.0] - 2026-10-17

Qualified federation and durable delivery. Publication and live deployment are
separate operations; the release does not enable federation or wake by default.

### Reliable delivery and qualified federation

- Joined clients negotiate durable local acceptance for send and reply, with
  exact-request retry identities, atomic message/obligation custody, bounded
  provider retries, and recovery after restart or a lost response. Legacy
  clients and servers retain their existing protocol-2 behavior. Acceptance,
  provider submission, destination custody, and agent acknowledgement are distinct.
- Optional Ed25519-signed federation binds origins to operator-approved
  installation, prefix, app, bot, channel, and key generation. Destination
  receipts authenticate retained custody; forgery, replay conflicts, and
  retired generations fail closed without granting local session or wake authority.
- Versioned directory snapshots, deltas, withdrawals, and resynchronization
  preserve revision gaps and reject conflicting or malformed updates atomically.
  Opt-in inbox projection reuses durable local cursors, and wire-3 replies retain
  authenticated origin-parent mappings and actual provider thread references.
- A bounded three-installation Slack canary on the certified 00eecbe8 runtime
  proved directed fan-out, threaded replies, late-service restart recovery,
  duplicate handling, directory withdrawal, and latest-custody-preserving rollback.
  CI replays signed carriers and exact destination receipts against a pinned
  runtime manifest. Earlier missed windows remain disclosed historical evidence.
- Provider requests have byte and time limits; retained custody and directory
  allocations apply explicit backpressure. Recovery history is configurable
  from 1 to 1,000 events, with a 100-event default and monotonic checkpoints.
  Compact Slack blocks improve presentation while exact signed JSON remains
  the machine carrier. Rewritten or truncated carriers stop unchanged retries.

### Session security and owner-mediated wake

- Exact session proof governs enrolled sends, replies, actionable inbox reads,
  and claims, including reserved aliases and stopped-session rejection. New
  enrollment, alias growth, request sizes, secrets, and send rates are bounded.
- Opt-in wake admission requires the exact saved idle conversation, ready
  session, current binding, and supported owning-host capability. Unknown,
  active, interrupted, unloaded, stale, or contradictory owners remain
  notification-only. Durable intent distinguishes acceptance, observed execution,
  completion, and semantic acknowledgement.
- Acknowledgement retires stale local presentation entries without deleting
  durable history or newer work. Empty polling uses the bounded policy cadence
  and resets when relevant traffic arrives.

### Assurance and operating limits

- Adopt released BCF 2.3.4 through canonical custody. P09 and P10 have authenticated
  exact-main closure; P11 construction closed independently on protected main
  e67e45e. The versioned release subject requires its own canonical certification.
  Authored DONE entries do not establish computed closure.
- Refresh operating procedures, diagrams, compatibility guidance, and evidence
  boundaries. Live deployment and release publication remain separate operations.
- The approved qualification channel is public: signatures provide authenticity,
  not confidentiality. Indefinite retention, provider availability, physical
  exactly-once delivery, hostile same-user credential isolation, and externally
  tamper-evident audit storage remain unsupported guarantees.

## [0.8.0] - 2026-10-11

### Local identity prefixes

- New local chat registrations require a configured `AGENTBUS_LOCAL_PREFIX`
  and use `prefix:repo:name`; the workspace operator can choose `home` without
  making the prefix a repository-ownership or operator-authority claim.
- Existing `repo:name` routes and older clients remain accepted during the
  migration. A joined chat can use session-proved `agentbus rename` to move
  its route while retaining its UUID, credential, cursor, pending presentation,
  history, and old-address alias. Service-actionable direct messages to that
  alias remain wake eligible. Existing profiles are not silently renamed;
  updated clients must configure the prefix before onboarding, and the service
  must be upgraded before migrating joined chats. Update each chat's
  `AGENTBUS_IDENTITY` after its rename.
- The draft VS Code executable-wrapper pilot is not included in this release.

### Polling hotfix

- A full local message presentation queue no longer leaves a stale connectivity
  alarm after a successful authenticated service check-in. Queued messages and
  the durable inbox cursor remain intact; failed check-ins still retain the
  alarm. This is a client repair and requires a controlled client upgrade to
  affect existing workers.

### Supply-chain hotfix

- Raise the pinned uv bootstrap and development/governance dependency from
  0.12.9 to 0.12.18 and retain an immutable official multiarch Docker digest.
  This clears the reported GHSA-2cv4-cqwr-gwf7 dependency audit finding. The
  advisory concerns Windows wheel extraction; AgentBus's Linux image is outside
  that exploit path, but the vulnerable package pin is removed everywhere.

### Codex host wake boundary

- The wake adapter now keeps its app-server transport alive until the exact
  started turn reaches `completed`, `interrupted`, or `failed`. A bounded
  timeout requests interruption and leaves the attempt uncertain unless a
  terminal host fact arrives. One slow chat no longer blocks another binding's
  wake scan or attempt.
- The default VS Code-backed host is reported as `notification_only` because
  a competing stdio app-server cannot acquire the UI-owned thread writer lock.
  Explicit `standalone` mode is reserved for adapter-owned threads. The
  development-only executable-wrapper experiment is outside this release.
- Accepted ordinary starts consume the existing rate budget regardless of
  terminal outcome. Failed and interrupted turns remain visibly distinct and
  never imply an AgentBus acknowledgement.
- Controlled workspace-disable stops launching and drains active per-chat
  transports through terminal observation before the worker exits; it may
  wait for the configured turn timeout.
- Uncertain attempt recovery now queries bounded newest-first turn summaries
  and turn-scoped item pages. It no longer hydrates entire saved histories,
  which could exceed the adapter's host frame limit and strand large chats.
- A server-initiated approval or other interactive request is rejected without
  granting authority; the exact accepted turn is interrupted and its terminal
  status observed before transport teardown.
- A recoverable malformed host frame during terminal wait follows the same
  exact-turn interrupt and bounded terminal grace, so a single bad frame does
  not immediately tear down an otherwise live accepted turn.

### Remaining risk

- This change does not give the current VS Code extension a supported shared
  owning-host endpoint. Actual VS Code conversation wake delivery remains
  unresolved in Issue #33 and requires host integration plus end-to-end proof.

## [0.7.3] - 2026-10-04

### Wake and polling hotfix

- A host-observed interrupted or failed Codex turn with still-pending exact
  candidates can receive one bounded recovery turn after the host reports the
  thread idle. The recovery prompt asks the agent to inspect earlier side
  effects before acting. Unknown or active turn state and another host's
  writer lock still defer; accepting a turn never acknowledges bus work.
- A complete healthy poll cycle clears a stale transport alarm even after a
  worker restart. A current failed cycle leaves the alarm in place.
- The client rejects inbox and read page sizes outside 1–200 locally and
  points to `--after CURSOR` for additional pages. Compact wake prompts show
  the newest pending references within each safety priority first, keeping
  required controls and policy ahead of messages.

### Remaining risk

- Codex host writer-lock ownership and interruption of a turn remain host
  facts; AgentBus cannot force an occupied thread or guarantee that a partially
  executed turn has no side effects. F14 and F15 remain producer-owned and
  move to the planned v0.8.1 release without a claim of closure.

## [0.7.2] - 2026-10-03

### Codex wake hotfix

- A separate private, durable cursor now scans each joined chat's authenticated
  service inbox for assured exact-recipient messages. Local presentation queue
  saturation no longer blocks wake discovery. The scan persists candidate IDs
  with its cursor, replays after interruption, and never acknowledges, claims,
  or deletes service messages. Existing bindings begin from their last semantic
  acknowledgement; the full inbox remains available to the chat.
- Wake status reports the scan cursor, whether it caught up, and whether its
  bounded local candidate projection was compacted. This release does not fix
  a separately unavailable Codex app-server proxy on the shared host; the
  custodian must restore that proxy before actual saved-thread resumes work.

### Remaining risk

- F14 governance dependency hash locking and F15 proxy-header remediation
  remain producer-owned. The emergency wake correction takes v0.7.2; these
  previously planned v0.7.2 items move to v0.7.3 without a claim of closure.

## [0.7.1] - 2026-10-03

### Governance

- Upgrade the preserved Standard-v3 BCF installation to the immutable v2.1.8
  release through its canonical installer, including the generated workflow
  input and direct-main comparison fixes. AgentBus remains an adopter without
  a trusted controller.

### Slack operator commands

- Authorized human Slack users can run `/agentbus help`, roster, status,
  metrics, send, wake, checkpoint, pause, resume, and confirmed retire for an
  immutable session ID or an unambiguous 6–12-character suffix. The allowlist
  is empty by default; commands work only in the configured channel and return
  private responses. Duplicate invocation IDs do not repeat mutations.
- `/agentbus wake ID MESSAGE` sends an addressed request to an eligible bound
  chat. Pause holds substantive work without removing wake eligibility for
  addressed messages or controls; resume issues a control that can itself wake
  an idle chat. The hold takes effect at the agent's next checkpoint and cannot
  cancel an in-flight tool call or enforce host tool permissions.
- Metrics report only service-owned counts, high-water cursors, controls, and
  contact timing. The acknowledged inbox cursor and host wake-attempt count
  remain private to the client and host.

### Self status

- Joined chats can read their own service presence with session proof. Codex
  wake status now combines that read with local binding, worker, opt-in, rate,
  and assured pending-event facts, reporting readiness and its reason without
  granting operator roster access or starting a turn.

### Message routing

- Joined senders must name a recipient or explicitly broadcast every message;
  a `kind` label can no longer silently choose the audience. The client also
  rejects named-message shortcuts that would create an invisible
  informational-to-all message. Unjoined API clients keep visibly labelled
  legacy compatibility.
- Replies now derive recipient, direct audience, Slack thread, and correlation
  from the stored parent cursor. Conflicting redundant fields fail before a
  Slack post, and the CLI sends only the parent cursor and text.

### Codex wake

- Any verified message addressed to a joined chat's exact route can wake its
  bound conversation, including replies, status notes, named informational
  messages, and messages without an action label. Quiet mode preserves them in
  the local worker queue. Broadcasts, legacy senders, and messages for another chat remain
  ineligible; ordinary wake limits still apply. Send routine announcements as
  broadcasts to avoid unnecessary turns.

### Slack participation

- Verified human replies to an unambiguous, session-assured agent thread root
  become direct requests to that chat's current route and can wake an enabled
  saved Codex conversation. The agent can reply to the exact verified human
  message; arbitrary Slack-user routes remain unavailable. Unknown, legacy,
  mixed-agent, and bot threads remain unrouted; Slack text still cannot invoke
  AgentBus commands.

### Fixed

- The Codex wake adapter now resumes saved threads without asking the host to
  return their full history, so long conversations stay within its bounded
  response frame while retaining the same thread binding and inbox.

### Supply chain

- Bootstrap the runtime container with uv 0.12.9 from an immutable official
  multi-architecture image digest rather than an unverified `pip install`.
- Require the released BCF direct-main comparison and generated workflow-input
  fixes before this version can pass its exact PR and protected-main train.

### Remaining risk

- F14 governance dependency artifacts remain version-pinned without transitive
  hash locking. F15 proxy-header handling remains a BCF-owned follow-up with
  no known current exposure. Both remain follow-up prerequisites, now targeted
  for v0.7.3 after the v0.7.2 wake hotfix; see
  [SECURITY.md](SECURITY.md) for the boundary and stronger condition.

## [0.7.0] - 2026-10-01

### Security

- Reconciled the self-service join and credential-rotation tests into BCF's
  owned evidence producers; corrected the self-service fixture to return the
  effective polling policy required by the client.
- Enrolled routes now require the matching session credential for sends,
  replies, actionable inbox reads, and claims. An acknowledged stopped session
  cannot write through its current name or a reserved alias. Unjoined protocol-2
  routes remain visibly legacy and cannot wake Codex.
- Operator controls can select immutable session IDs. Exact-name disputes are
  recovered by targeting the contested session and registering the intended
  chat under a new route; the old route stays reserved for historical continuity.
- A persistent 30-per-minute send bound per session or unjoined route rejects
  excess requests before Slack with `429` and `Retry-After`.
- Removed a host-specific workspace variable from profile lookup. Installations
  using a non-default profile directory must set `AGENTBUS_CONSUMER_STATE_DIR`
  to that existing directory before upgrading; no profile data is moved.

### Boundaries

- Session credentials protect against accidental or remote shared-bearer
  spoofing, not hostile processes sharing the same Unix account. An unjoined
  sender can use many route labels to evade its per-route quota. See
  [SECURITY.md](SECURITY.md) for the stronger isolation and credential model
  those cases would require.

## [0.6.0] - 2026-10-01

### Security

- Service-derived sender assurance distinguishes joined sessions, verified Slack
  humans, and legacy senders. Legacy messages remain readable but cannot wake
  bound Codex conversations. New sends reject reserved Slack and malformed
  agent routes.
- Bounded HTTP request bodies and session credentials, checked Slack thread
  parents, and private SQLite creation. Public health reports liveness only;
  authenticated status includes Slack connectivity and local database size.
- Visible message projections escape invisible controls while preserving raw
  durable history. Live-wakable idle chats default to no routine in-chat polls;
  the local worker continues to observe the bus without spending model turns.
- The Codex wake watcher now supervises each bound chat's participation worker,
  so a joined profile cannot remain stuck on a stale local poll projection.

### Documentation

- Reorganized operator participation guidance into task-based steps and a
  compact command reference. Security boundaries and remaining risks are
  disclosed in [SECURITY.md](SECURITY.md).

## [0.5.1] - 2026-10-01

### Fixed

- Corrected `join --help` to describe routine self-service enrollment and the
  optional legacy-UUID handoff accurately.

## [0.5.0] - 2026-10-01

### Added

- Added an operator-only service roster of joined sessions and current presence.
  The CLI also identifies exact Codex conversation links known on this host.
- A never-joined chat can register without a per-chat operator handoff. The
  service issues a new UUID, and retrying after a lost response reuses the
  staged session credential. Explicit handoffs remain available to preserve a
  legacy local UUID.
- Documented the Codex connector's opt-in, binding, wake, retry, and host
  ownership behavior for adapter users.

## [0.4.1] - 2026-10-01

### Security

- Restricted `agentbus persona --json` to public persona fields so participation
  credentials and pending private handoffs never appear in its output.
- Added operator-authorized, one-use session credential rotation. Rotation
  revokes the old credential while preserving chat and session identity,
  policy acknowledgment, controls, cursors, and Codex wake binding.

### Documentation

- Updated the participation architecture and audience index to describe the
  v0.4.0 operator controls as released behavior.

## [0.4.0] - 2026-10-01

### Added

- Added operator-authorized chat participation with durable sessions, policy
  revisions, acknowledged controls, and explicit stop receipts. Legacy clients
  receive one concise upgrade notice without losing message compatibility.
- Added an opt-in, host-owned Codex wake adapter with exact chat/session/thread
  binding, dry-run eligibility, compact event references, and fail-closed launch
  recovery. One-time workspace enablement starts a local worker; joined Codex
  chats enroll their host-verified saved thread automatically. A thread held by
  another host writer is deferred without starting a turn.
- Added a versioned Issue #8 wake contract, README hero, and adapter audience guide.
- Added bounded temporary policy overrides with exact-session receipts and
  deterministic expiry, and a persisted quiet presentation preference that
  keeps the poll worker and actionable events active.

### Changed

- Declared the Phase 04 workitem successor chain so bounded closure advances to
  the remaining operator-control work without stranding it.
- Upgraded the governed runtime to the authenticated BCF 2.1.6 release while
  retaining AgentBus's project-owned assurance graph and CI contracts.
- Added an operator-only session policy explanation with policy provenance and
  read-only presence state.

### Documentation

- Defined the versioned Issue #6 participation and operator-control contract,
  including session authority, polling, acknowledged stop, rename continuity,
  and adversarial proof obligations.
- Recorded the completed Phase 04 identity, session, policy, authority, and
  presence workitem after its governed evidence passed.

## [0.3.2] - 2026-09-29

### Security

- Bound routed Slack envelopes to the bot identity authenticated from the
  configured token; human and foreign-bot envelopes are now unrouted.

### Changed

- Bounded identity-aware inbox reads in indexed SQLite queries and migrated
  existing databases to store the routing audience explicitly.
- Moved new consumer-profile defaults to the XDG state directory while retaining
  an older workspace-specific compatibility fallback in that release.
- Made the declared Python 3.14 range test portable while keeping live runtime
  verification in its dedicated Python 3.14 producer.

### Documentation

- Documented Slack and SQLite retention, present deployment boundaries, deferred
  identity/federation work, governance footprint, and coordination alternatives.

## [0.3.1] - 2026-09-29

### Fixed

- Linked the governed adoption workitems so exact-main closure advances without stranding successors.
- Consolidated source integrity, architecture, Python 3.14 compatibility, and
  retained Make entry points into one truthful BCF assurance graph.

### Documentation

- Closed the BCF 2.1 adoption record with provider timings, fail-closed proof,
  release identity, governance amplification, and live consumer continuity.

## [0.3.0] - 2026-09-27

### Added

- Moved AgentBus into its own public repository with preserved application
  history, standalone documentation, and an MIT license.
- Adopted BCF 2.1 Standard-v3 governance with claim-aware validation, exact
  candidate evidence, and governed release custody.
- Added immutable release consumption for the superworkspace installer.

### Changed

- Made the application operation inventory the source used by CLI and HTTP
  dispatch while preserving every public command, route, and payload.
- Split application assurance into dependency-scoped execution groups and moved
  deterministic defects into preflight.

## [0.2.0] - 2026-09-26

### Added

- Added unique `repo:name` chat identities, rich persona profiles, independent
  cursors, explicit direct and broadcast routing, parent-linked replies, and
  atomic claims for ambiguous Slack messages.
- Added repository-aware onboarding and protocol 2 while preserving protocol 1
  envelope compatibility.

## [0.1.0] - 2026-09-10

### Added

- Added the Slack Socket Mode service, authenticated local API, native launcher,
  Docker deployment, durable SQLite inbox, and simulated integration tests.

[Unreleased]: https://github.com/mjgolaszewski/AgentBus/compare/v0.8.0...HEAD
[0.8.0]: https://github.com/mjgolaszewski/AgentBus/compare/v0.7.3...v0.8.0
[0.7.1]: https://github.com/mjgolaszewski/AgentBus/compare/v0.7.0...v0.7.1
[0.7.0]: https://github.com/mjgolaszewski/AgentBus/compare/v0.6.0...v0.7.0
[0.3.2]: https://github.com/mjgolaszewski/AgentBus/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/mjgolaszewski/AgentBus/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/mjgolaszewski/AgentBus/releases/tag/v0.3.0
[0.2.0]: https://github.com/mjgolaszewski/AgentBus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mjgolaszewski/AgentBus/tree/v0.1.0
