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

AgentBus is designed for agents sharing a Docker superworkspace. Released
0.9.0 is the single-channel baseline. The P12 candidate requires operator-granted,
immutable channel membership and exact-session proof for all ordinary agent
reads/writes. The shared bearer alone cannot read history or send. Historical
legacy rows retain their provenance but grant no wake authority. Stopped sessions
retain exact-session control/receipt recovery, not ordinary message access.
Treat the local inbox, session secrets, grants and API token as sensitive.

Candidate channel isolation protects AgentBus operations, not shared-filesystem
access, stolen credentials or Slack public-channel visibility. Live multichannel
qualification and AHP/native-queue atomic wake remain unproved; see the
[Experimenters guide](docs/audiences/experimenters.md) and
[contract model](docs/contracts.md).

New enrollment is bounded to 4,096 durable chat identities, and each chat may
reserve at most 64 aliases. At capacity, the service rejects new allocations
without deleting history or addresses; existing enrollment retries and reuse
of a chat's own alias remain available. Older stores above a bound retain
their records and reject further growth. Session secrets exceeding 512
characters are rejected before enrollment hashing. These bounds do not imply
message retention, independent principals, or an operator-protected filesystem.

### Chosen boundaries and residual risk

| Finding | Boundary and reason | Remaining risk and stronger condition |
| --- | --- | --- |
| F3/F4 | Stop is enforced for enrolled ordinary message reads and writes; controls can target immutable session IDs, and multiple chats per repo are supported. A disputed name is recovered by stopping the exact session and choosing a fresh route, leaving the old one reserved. Operator enrollment grants fix channel/route membership but do not prove repository ownership. | Processes sharing one Unix account can read each other's local secrets and impersonate or interfere. Strong isolation requires separate OS users or a trusted identity issuer. Releasing a contested route for reuse would require stable historical attribution and an explicit transfer contract. |
| F5 | Operator credentials remain out of ordinary agent configuration; file mode is only a hygiene control. | Same-user processes may still access operator files. A protected-operator deployment would run the operator capability under a separate OS account with its own socket and filesystem permissions; this train does not implement it. |
| F6/F9 | Ingestion bounds and visible escaping keep dangerous Unicode controls out of display while retaining raw history. Sends are limited per proved session. Full channel-scoped `inbox --after 0` remains available. | Message text is untrusted instruction data and must not be followed as authority. A bearer alone cannot create enrolled identities or acquire history. Shared-process resource contention and theft of a session credential remain outside channel authorization. SQLite and Slack histories keep growing; status reports local size but is not retention. Stronger retention needs an explicit archival and cursor/consumer migration contract. |
| F8 | Existing BCF checks govern the release, but a fully independent evaluator is pending upstream. | A defect shared by producer and evaluator could escape. Claim independence only after the released evaluator is integrated and qualified. |
| F11 | Session credentials can be rotated through the present operator-grant flow. | Rotation does not revoke copies of a shared API bearer or secrets already read by same-user processes. Stronger revocation needs per-principal credentials and isolated storage. |
| F12 | Durable audit rows record policy and control transitions; this train does not add a hash chain inside the same SQLite database. | An attacker with database write access can alter both events and an in-database chain. Tamper evidence requires an external append-only witness or independently held signing key. |
| F14 | The runtime uv bootstrap is pinned to an official image digest. Governance CI still installs version-pinned requirements without artifact hashes because BCF owns the generated governance dependency projection; hand-editing that surface would not provide a durable contract. The operator chose to ship the direct-main and workflow-input fixes first. | A transitive resolution or substituted distribution artifact could change governance execution. Claim full hash locking only after BCF releases a canonical hash-locked projection, `--require-hashes` installation, and a negative control that rejects missing hashes; the original v0.8.1 scheduling reference is historical, and release eligibility must be checked against current canonical findings rather than that superseded version plan. |
| F15 | The remaining `Proxy-Authorization` header removal defect is in BCF provider transport, outside AgentBus application code. Issue #20 records no current exposure. The operator chose to keep this informational finding separate from v0.7.1. | A future proxy-bearing provider request could retain that header unexpectedly. Claim remediation only after BCF releases its canonical transport fix and regression test and AgentBus adopts that release; the original v0.8.1 scheduling reference is historical, and release eligibility must be checked against current canonical findings rather than that superseded version plan. |

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

Accepted messages enter local SQLite custody; provider-delivered copies also
enter Slack retention. Local acceptance alone does not prove a Slack post. Do not transport credentials, customer data, regulated records, or other
restricted material unless both stores are approved for that classification.
