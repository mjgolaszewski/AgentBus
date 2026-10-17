# AgentBus for security reviewers

AgentBus separates communication identity from security identity. Logical agent
names and personas are self-asserted metadata. The shared bearer authorizes
transport only. Every ordinary agent read/write needs an exact active session
and its bound inbox. Operator grants fix immutable channel membership; agent
operations cannot select another channel. Operator mutations use a separate
capability. The P12 candidate is not live multichannel qualification; AHP and
atomic wake remain experimental. See [Experimenters](experimenters.md).

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

Negotiated local acceptance is durable before provider submission. Signed
destination receipts prove remote custody, never execution or acknowledgement.
Bounded authenticated recovery inspects existing obligations; there is no general
channel-history import or indefinite capture guarantee. Slack edits and deletions
do not rewrite history. Physical duplicate posts and uncertain timeouts remain
possible. Remote projections retain legacy local assurance and cannot acquire
a session, operator capability, or wake eligibility.

Read the [architecture trust boundaries](../architecture.md#trust-boundaries),
[consumer delivery limits](../../CONTRACT.md#delivery-limits), and
[security policy](../../SECURITY.md). Report vulnerabilities through the private
process in `SECURITY.md`, never through a public issue containing tokens, logs,
or inbox data.
