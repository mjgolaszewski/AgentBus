# AgentBus for maintainers

AgentBus uses BCF Standard profile contract v3 because routing, cursor custody,
Slack ingestion, release construction, and consumer compatibility require
behavioral evidence tied to exact candidate bytes.

Project code and contracts own AgentBus semantics. BCF owns its installed
runtime, schemas, generated workflows, evidence planning, and eligibility
machinery. The repository retains direct project authority and does not adopt a
persistent trusted controller.

## Working path

- Change dependency declarations and `uv.lock` together.
- Keep tests simulated; never place live Slack credentials in CI.
- Add proof according to the proposition it establishes, not its directory or a
  convenient shard size.
- Reconcile generated governance surfaces instead of editing them directly.
- Submit semantic intent through `bcf ci submit --repo-root . --intent workitem`.
- Build and release only from the exact protected subject proved by the governed
  train.

See the [governance model](../governance-model.md), [adoption measurements](../adoption-measurements.md),
[operations runbook](../OPERATIONS.md), and root [development commands](../../README.md#development).
