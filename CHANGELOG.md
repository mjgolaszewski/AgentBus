# Changelog

All notable changes to AgentBus are documented here. AgentBus follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
  `WEED_WORKSPACE` as a compatibility fallback for existing deployments.
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

[Unreleased]: https://github.com/mjgolaszewski/AgentBus/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/mjgolaszewski/AgentBus/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/mjgolaszewski/AgentBus/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/mjgolaszewski/AgentBus/releases/tag/v0.3.0
[0.2.0]: https://github.com/mjgolaszewski/AgentBus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mjgolaszewski/AgentBus/tree/v0.1.0
