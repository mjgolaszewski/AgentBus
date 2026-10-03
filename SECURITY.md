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
From v0.7.0, enrolled routes require session proof for sending and actionable
operations, and acknowledged stopped sessions cannot write. Treat the local
inbox, session secrets, and API token as sensitive.

### Chosen boundaries and residual risk

| Finding | Boundary and reason | Remaining risk and stronger condition |
| --- | --- | --- |
| F3/F4 | Stop is enforced for enrolled message writes; controls can target immutable session IDs, and multiple chats per repo are supported. A disputed name is recovered by stopping the exact session and choosing a fresh route, leaving the old one reserved. Self-service enrollment cannot prove repository ownership. | Processes sharing one Unix account can read each other's local secrets and impersonate or interfere. Strong isolation requires separate OS users or a trusted identity issuer. Releasing a contested route for reuse would require stable historical attribution and an explicit transfer contract. |
| F5 | Operator credentials remain out of ordinary agent configuration; file mode is only a hygiene control. | Same-user processes may still access operator files. A protected-operator deployment would run the operator capability under a separate OS account with its own socket and filesystem permissions; this train does not implement it. |
| F6/F9 | Ingestion bounds and visible escaping keep dangerous Unicode controls out of display while retaining raw history. Sends are limited per joined session or unjoined route. Full `inbox --after 0` remains a compatibility contract. | Message text is untrusted instruction data and must not be followed as authority. An API bearer holder can evade the legacy per-route limit by creating many unjoined labels; global or per-principal quotas need distinct credentials. SQLite and Slack histories keep growing; status reports local size but is not retention. Stronger retention needs an explicit archival and cursor/consumer migration contract. |
| F8 | Existing BCF checks govern the release, but a fully independent evaluator is pending upstream. | A defect shared by producer and evaluator could escape. Claim independence only after the released evaluator is integrated and qualified. |
| F11 | Session credentials can be rotated through the present operator-grant flow. | Rotation does not revoke copies of a shared API bearer or secrets already read by same-user processes. Stronger revocation needs per-principal credentials and isolated storage. |
| F12 | Durable audit rows record policy and control transitions; this train does not add a hash chain inside the same SQLite database. | An attacker with database write access can alter both events and an in-database chain. Tamper evidence requires an external append-only witness or independently held signing key. |
| F14 | The runtime uv bootstrap is pinned to an official image digest. Governance CI still installs version-pinned requirements without artifact hashes because BCF owns the generated governance dependency projection; hand-editing that surface would not provide a durable contract. The operator chose to ship the direct-main and workflow-input fixes first. | A transitive resolution or substituted distribution artifact could change governance execution. Claim full hash locking only after BCF releases a canonical hash-locked projection, `--require-hashes` installation, and a negative control that rejects missing hashes; this is a v0.7.3 prerequisite after the urgent v0.7.2 wake hotfix. |
| F15 | The remaining `Proxy-Authorization` header removal defect is in BCF provider transport, outside AgentBus application code. Issue #20 records no current exposure. The operator chose to keep this informational finding separate from v0.7.1. | A future proxy-bearing provider request could retain that header unexpectedly. Claim remediation only after BCF releases its canonical transport fix and regression test and AgentBus adopts that release; this is a v0.7.3 prerequisite after the urgent v0.7.2 wake hotfix. |

Routed Slack envelopes are trusted only when Slack attributes them to the bot
identity authenticated from the configured bot token. Envelope-shaped messages
from humans and other bots remain unrouted input.

A verified human's reply to a thread rooted in one session-proved agent message
is routed as a request to that chat. This makes any human who can post in the
channel able to request a wake of an enabled chat, subject to the host's opt-in,
stopped-session rule, and wake budget; it grants no operator control or new tool
permission. Slack supplies the thread root, not an exact child-message parent.
Mixed-agent, unknown-root, legacy-root, and bot replies stay unrouted. If
channel membership is too broad for this boundary, restrict the channel or add
separately authenticated per-human wake policy before enabling the adapter.

Every message sent through AgentBus enters Slack retention and the local SQLite
inbox. Do not transport credentials, customer data, regulated records, or other
restricted material unless both stores are approved for that classification.
