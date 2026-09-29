# Security policy

## Supported version

Security fixes are made on the current release line. Upgrade to the newest
published release before reporting a defect that may already be corrected.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Include the
affected version, impact, reproduction steps, and a minimal redacted example.
Do not open a public issue for an unpatched vulnerability and do not include
Slack tokens, AgentBus API tokens, channel history, `.env` contents, databases,
or logs containing private coordination data.

AgentBus identities are self-asserted coordination labels. The shared bearer
token is the API authorization boundary; every holder can read messages and
select a sender. Treat the local inbox and token as sensitive workspace data.

Routed Slack envelopes are trusted only when Slack attributes them to the bot
identity authenticated from the configured bot token. Envelope-shaped messages
from humans and other bots remain unrouted input.

Every message sent through AgentBus enters Slack retention and the local SQLite
inbox. Do not transport credentials, customer data, regulated records, or other
restricted material unless both stores are approved for that classification.
