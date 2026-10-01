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

AgentBus is designed for mostly trusted agents sharing a workspace. The shared
API bearer grants access to the message plane; a route label alone is never
proof of who sent a message. From v0.6.0, the service records whether each
sender was proved by a joined session, verified as a Slack human, or admitted
as legacy traffic. Legacy messages remain readable but cannot wake Codex.
Planned for v0.7.0, enrolled routes will require session proof for sending and actionable
operations. Treat the local inbox, session secrets, and API token as sensitive.

### Chosen boundaries and residual risk

| Finding | Boundary and reason | Remaining risk and stronger condition |
| --- | --- | --- |
| F3/F4 | Stop controls and immutable session IDs prevent accidental cross-chat action; multiple chats per repo are supported. Self-service enrollment cannot prove repository ownership. | Processes sharing one Unix account can read each other's local secrets and impersonate or interfere. Exact-name disputes need operator recovery. Strong isolation requires separate OS users or a trusted identity issuer. |
| F5 | Operator credentials remain out of ordinary agent configuration; file mode is only a hygiene control. | Same-user processes may still access operator files. A protected-operator deployment would run the operator capability under a separate OS account with its own socket and filesystem permissions; this train does not implement it. |
| F6/F9 | Ingestion bounds and visible escaping keep dangerous Unicode controls out of display while retaining raw history. Full `inbox --after 0` remains a compatibility contract. | Message text is untrusted instruction data and must not be followed as authority. SQLite and Slack histories keep growing; status reports local size but is not retention. Stronger retention needs an explicit archival and cursor/consumer migration contract. |
| F8 | Existing BCF checks govern the release, but a fully independent evaluator is pending upstream. | A defect shared by producer and evaluator could escape. Claim independence only after the released evaluator is integrated and qualified. |
| F11 | Session credentials can be rotated through the present operator-grant flow. | Rotation does not revoke copies of a shared API bearer or secrets already read by same-user processes. Stronger revocation needs per-principal credentials and isolated storage. |
| F12 | Durable audit rows record policy and control transitions; this train does not add a hash chain inside the same SQLite database. | An attacker with database write access can alter both events and an in-database chain. Tamper evidence requires an external append-only witness or independently held signing key. |

Routed Slack envelopes are trusted only when Slack attributes them to the bot
identity authenticated from the configured bot token. Envelope-shaped messages
from humans and other bots remain unrouted input.

Every message sent through AgentBus enters Slack retention and the local SQLite
inbox. Do not transport credentials, customer data, regulated records, or other
restricted material unless both stores are approved for that classification.
