# Changelog

All notable changes to AgentBus are documented here. AgentBus follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Security

- Bound routed Slack envelopes to the bot identity authenticated from the
  configured token; human and foreign-bot envelopes are now unrouted.

### Changed

- Bounded identity-aware inbox reads in indexed SQLite queries and migrated
  existing databases to store the routing audience explicitly.
- Moved new consumer-profile defaults to the XDG state directory while retaining
  `WEED_WORKSPACE` as a compatibility fallback for existing deployments.
- Made the default test suite skip the dedicated Python 3.14 qualification node
  when it runs under another supported interpreter.

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

[Unreleased]: https://github.com/mjgolaszewski/AgentBus/compare/v0.3.1...HEAD
[0.3.1]: https://github.com/mjgolaszewski/AgentBus/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/mjgolaszewski/AgentBus/releases/tag/v0.3.0
[0.2.0]: https://github.com/mjgolaszewski/AgentBus/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/mjgolaszewski/AgentBus/tree/v0.1.0
