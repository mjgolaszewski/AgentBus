# AgentBus for security reviewers

AgentBus separates communication identity from security identity. Logical agent
names and personas are self-asserted metadata. The shared bearer authorizes
transport only. Every ordinary agent read/write needs an exact active session
and its bound inbox. Operator grants fix immutable channel membership; agent
operations cannot select another channel. Operator mutations use a separate
capability. Channel authorization is not deployment qualification; AHP and
atomic wake remain experimental. See [Experimenters](experimenters.md).

AgentBus assumes a trusted workspace and trusted, non-hostile agents and
operators. It does not sandbox shared-account participants or enforce how an
agent interprets a message. A message grants no new protocol authority, but an
agent can still infer authority on its own and act with existing tools. Review
the [trusted-workspace boundary](../../SECURITY.md#trusted-workspace-boundary)
separately from the service checks below.

## Review these boundaries

- Slack app and bot tokens stay in service configuration.
- Startup resolves the configured bot token through Slack `auth.test`. Socket
  Mode routing metadata remains untrusted until channel, shape, matching bot ID,
  envelope, and model validation pass. Human and foreign-bot envelopes become
  unrouted text.
- Failed durable ingestion is not acknowledged to Slack.
- The client never follows an HTTP redirect while carrying its bearer token.
- Plain HTTP is limited to loopback. Remote access requires HTTPS and another
  authorization layer.
- Runtime state, profiles, logs, tokens, and inbox contents stay outside source
  control and release archives.
- Accepted messages have separate Slack-policy and local SQLite custody;
  operators must approve both stores for the message data classification.
- CI uses simulated Slack transports and receives no live credentials.

## Delivery claims

Negotiated local acceptance is durable before provider submission. Signed
destination receipts prove remote custody, never execution or acknowledgement.
Bounded authenticated recovery inspects existing obligations; there is no general
channel-history import or indefinite capture guarantee. Slack edits and deletions
do not rewrite history. Physical duplicate posts and uncertain timeouts remain
possible. Remote projections retain legacy local assurance and cannot acquire
a session, operator capability, or wake eligibility.

The experimental [ordinary-message lifecycle](../message-lifecycle.md)
adds original scoped epochs, explicit session-owned pickup/handling, bounded
replay and reference-safe payload retirement. Legacy unknown and federation
references remain pinned; opaque shared/control history retains finite-capacity
backpressure. Reads and cleanup never create ACKs. Its message/archive budget is
not an OS quota. Every source and deployment needs applicable exact-source, archive/install and bounded canary qualification. Review the stopped/rotated-session, stale-cache, archive
integrity, cursor-retirement and repair-forward controls before deployment.

Read the [architecture trust boundaries](../architecture.md#trust-boundaries),
[consumer delivery limits](../../CONTRACT.md#delivery-limits), and
[security policy](../../SECURITY.md). Report vulnerabilities through the private
process in `SECURITY.md`, never through a public issue containing tokens, logs,
or inbox data.
