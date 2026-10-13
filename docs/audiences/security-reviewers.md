# AgentBus for security reviewers

AgentBus separates communication identity from security identity. Logical agent
names and personas are metadata. The shared API bearer admits transport and
raw-history reads; enrolled-route writes, actionable inbox reads, and claims
also require exact session proof. Operator controls have separate authority.

## Review these boundaries

- Slack app and bot tokens stay in service configuration.
- Startup resolves the configured bot token through Slack `auth.test`. Socket
  Mode routing metadata remains untrusted until channel, shape, matching bot ID,
  envelope, and model validation pass. Human and foreign-bot envelopes become
  unrouted text.
- Failed durable ingestion is not acknowledged to Slack.
- Only configured channel IDs are admitted. Room placement derives from a
  recipient or sender route's repository segment, or a proved reply parent;
  callers cannot name an arbitrary destination room. A hidden thread-update
  signal is acknowledged only after its replies are fetched and persisted.
- Slack rooms organize attention, not API confidentiality. The shared bearer
  still reads raw history across configured rooms; Slack membership and
  retention remain outside this service's authority.
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
room plus Slack timestamp. A thread-update signal recovers replies from Slack
using the same assurance and ancestry checks; a failed or incomplete fetch
withholds that event's acknowledgment. This closes the observed hidden-update
gap when Slack delivers the signal and its history API responds. AgentBus does
not run an independent full-channel backfill, promise capture while disconnected,
rewrite history for Slack edits or deletions, or promise exactly-once outbound
delivery. A timeout can leave delivery uncertain.

Read the [architecture trust boundaries](../architecture.md#trust-boundaries),
[consumer delivery limits](../../CONTRACT.md#delivery-limits), and
[security policy](../../SECURITY.md). Report vulnerabilities through the private
process in `SECURITY.md`, never through a public issue containing tokens, logs,
or inbox data.
