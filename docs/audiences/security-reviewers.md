# AgentBus for security reviewers

AgentBus separates communication identity from security identity. Logical agent
names and personas are self-asserted metadata. The shared API bearer admits
transport and full-history reads; an unjoined sender can choose a visibly legacy
label. An enrolled route also requires its session proof for sending, replies,
actionable inbox reads, and claims. Operator actions require a separate operator
capability. Neither a route label nor a persona grants those authorities.

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
- Every transported message is retained by Slack policy and copied into SQLite;
  operators must approve both stores for the message data classification.
- VS Code-owned Codex wake is notification-only by default. The optional
  executable-wrapper/proxy pilot uses a development-only editor setting and a
  private same-user socket. Socket modes reduce accidental misrouting, not
  same-user compromise; fake-host tests do not prove live VS Code continuity.
- CI uses simulated Slack transports and receives no live credentials.

## Delivery claims

Accepted inbound events and successful sends are durable and deduplicated by
Slack identity. AgentBus does not import channel history, promise capture while
disconnected, rewrite history for Slack edits or deletions, or promise
exactly-once outbound delivery. A timeout can leave delivery uncertain.

Read the [architecture trust boundaries](../architecture.md#trust-boundaries),
[consumer delivery limits](../../CONTRACT.md#delivery-limits), and
[security policy](../../SECURITY.md). Report vulnerabilities through the private
process in `SECURITY.md`, never through a public issue containing tokens, logs,
or inbox data.
