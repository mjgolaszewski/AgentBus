# AgentBus for security reviewers

AgentBus separates communication identity from security identity. Logical agent
names and personas are self-asserted metadata. The local bearer token authorizes
all `/v1` operations and allows its holder to choose a sender.

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
