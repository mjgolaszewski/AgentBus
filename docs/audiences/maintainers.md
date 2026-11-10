# AgentBus for maintainers

AgentBus uses BCF Standard profile contract v3 because routing, cursor custody,
Slack ingestion, release construction, and consumer compatibility require
behavioral evidence tied to exact candidate bytes.

Project code and contracts own AgentBus semantics. BCF owns its installed
runtime, schemas, generated workflows, evidence planning, and eligibility
machinery. The repository retains direct project authority and does not adopt a
persistent trusted controller.

Channel-isolated operation has bounded local two-channel evidence tied to its
protected source. Each changed subject still needs applicable qualification. AHP/native-queue
interoperability and atomic wake remain separate, unqualified claims.
Use the [contract model](../contracts.md) and [Experimenters guide](experimenters.md)
when reviewing capability descriptions. The editorial audit records per-document
line coverage and dispositions rather than inferring review from link checks.

Review optional publishing, local A2A, access observations and automatic
retention against their versioned owners in the [contract model](../contracts.md).
Disabled optional features add no periodic provider work. A2A completion means
admission, publication means an explicit named update, and automatic retention
requires actual semantic ACK plus existing custody pins. Public A2A and atomic
owner wake remain unqualified.

## Working path

- Change dependency declarations and `uv.lock` together.
- Keep tests simulated; never place live Slack credentials in CI.
- Add proof according to the proposition it establishes, not its directory or a
  convenient shard size.
- Reconcile generated governance surfaces instead of editing them directly.
- Include a capability-oriented `CHANGELOG.md` update in every pull request.
- Use the installed released BCF 2.3.4 interpreter and a clean candidate.
- Let canonical submission derive intent and required proof through `bcf ci submit --repo-root .`.
- Build and release only from the exact protected subject proved by the governed
  train.

See the [governance model](../governance-model.md), [adoption measurements](../adoption-measurements.md),
[operations runbook](../OPERATIONS.md), and root [development commands](../../README.md#development).
