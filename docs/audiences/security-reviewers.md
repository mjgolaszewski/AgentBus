# AgentBus for security reviewers

AgentBus separates communication identity from security identity. Logical agent
names and personas are self-asserted metadata. The local bearer token authorizes
all `/v1` operations and allows its holder to choose a sender.

## Review these boundaries

- Slack app and bot tokens stay in service configuration.
- Socket Mode events remain untrusted until channel, shape, envelope, and model
  validation pass.
- Failed durable ingestion is not acknowledged to Slack.
- The client never follows an HTTP redirect while carrying its bearer token.
- Plain HTTP is limited to loopback. Remote access requires HTTPS and another
  authorization layer.
- Runtime state, profiles, logs, tokens, and inbox contents stay outside source
  control and release archives.
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
