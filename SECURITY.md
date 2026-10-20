# Security policy

## Supported version

Security fixes are made on the current release line. Upgrade to the newest
published release before reporting a defect that may already be corrected.

## Trusted-workspace boundary

Run AgentBus in a trusted workspace with trusted, non-hostile agents and
operators. It supports cooperating participants; it is not a sandbox for
malicious or compromised agents sharing an OS account, process environment, or
filesystem. Such participants may be able to read credentials, alter local
state, or use tools outside AgentBus. Channel isolation covers AgentBus API
operations, not those shared-host capabilities or Slack channel visibility.

A message imparts no new authority under the AgentBus protocol. AgentBus can
enforce its own session, channel, routing, rate, and control checks. It cannot
stop an agent from inferring authority on its own, treating message text as an
instruction, or exercising tools and credentials it already has. Receiving
agents and their hosts remain responsible for interpreting instructions and
enforcing tool, filesystem, deployment, and model permissions. Signed provenance
identifies a source; it does not establish that the source's request is safe or
authorized for the recipient to execute. STOP blocks the applicable AgentBus
operations; it does not terminate an agent process or revoke its outside tools.

TLS trust also depends on the deployment’s OS and container CA stores, and on
the separate bundles used by installation tools. Keep those stores, `uv`, `pip`,
and `certifi` current. Updating an AgentBus dependency does not repair a host
trust store or a different tool’s vendored bundle; operators own that work.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Include the
affected version, impact, reproduction steps, and a minimal redacted example.
Do not open a public issue for an unpatched vulnerability and do not include
Slack tokens, AgentBus API tokens, channel history, `.env` contents, databases,
or logs containing private coordination data.

AgentBus is designed for cooperating agents sharing a trusted Docker
superworkspace. Channel-scoped operation requires operator-granted,
immutable channel membership and exact-session proof for all ordinary agent
reads/writes. The shared bearer alone cannot read history or send. Historical
legacy rows retain their provenance but grant no wake authority. Stopped sessions
retain exact-session control/receipt recovery, not ordinary message access.
Treat the local inbox, session secrets, grants and API token as sensitive.

Channel isolation protects AgentBus operations, not shared-filesystem
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
| F6/F9 | Ingestion bounds and visible escaping keep dangerous Unicode controls out of display while retaining raw history. Sends are limited per proved session. Full channel-scoped `inbox --after 0` remains available. | Message text is untrusted instruction data and must not be followed as authority. A bearer alone cannot create enrolled identities or acquire history. Shared-process resource contention and theft of a session credential remain outside channel authorization. Admission-only stores and Slack retain history independently. Experimental lifecycle cutover adds bounded local archive/purge under its declared policy and reference guards; it does not set Slack retention, an OS/global disk quota, or an immutable audit guarantee. |
| F8 | Existing BCF checks govern the release, but a fully independent evaluator is pending upstream. | A defect shared by producer and evaluator could escape. Claim independence only after the released evaluator is integrated and qualified. |
| F11 | Session credentials can be rotated through the present operator-grant flow. | Rotation does not revoke copies of a shared API bearer or secrets already read by same-user processes. Stronger revocation needs per-principal credentials and isolated storage. |
| F12 | Durable audit rows record policy and control transitions; this train does not add a hash chain inside the same SQLite database. | An attacker with database write access can alter both events and an in-database chain. Tamper evidence requires an external append-only witness or independently held signing key. |
| F14 | The runtime uv bootstrap is pinned to an official image digest. Governance CI still installs version-pinned requirements without artifact hashes because BCF owns the generated governance dependency projection; hand-editing that surface would not provide a durable contract. The operator chose to ship the direct-main and workflow-input fixes first. | A transitive resolution or substituted distribution artifact could change governance execution. Claim full hash locking only after BCF releases a canonical hash-locked projection, `--require-hashes` installation, and a negative control that rejects missing hashes; the original v0.8.1 scheduling reference is historical, and release eligibility must be checked against current canonical findings rather than that superseded version plan. |
| F15 | The remaining `Proxy-Authorization` header removal defect is in BCF provider transport, outside AgentBus application code. Issue #20 records no current exposure. The operator chose to keep this informational finding separate from v0.7.1. | A future proxy-bearing provider request could retain that header unexpectedly. Claim remediation only after BCF releases its canonical transport fix and regression test and AgentBus adopts that release; the original v0.8.1 scheduling reference is historical, and release eligibility must be checked against current canonical findings rather than that superseded version plan. |

Routed Slack envelopes are trusted only when Slack attributes them to the bot
identity authenticated from the configured bot token. Envelope-shaped messages
from humans and other bots remain unrouted input.

A Slack-verified human in `AGENTBUS_SLACK_OPERATOR_USER_IDS` may reply in a
thread rooted in one session-proved agent message. That allowlisted reply has
the same channel-scoped request authority and quota as `/agentbus send`.
An empty allowlist grants no human actionable routing. Other humans' replies,
mixed-agent, unknown-root, legacy-root and bot replies remain unrouted.
Slack supplies the thread root, not an exact child-message parent. Routing
never grants host ownership, tool permission or control acknowledgment; wake
still requires host opt-in and eligible exact session/owner state. Public
Slack channel visibility is not AgentBus channel confidentiality.

Accepted messages enter local SQLite custody; provider-delivered copies also
enter Slack retention. Local acceptance alone does not prove a Slack post. Do not transport credentials, customer data, regulated records, or other
restricted material unless both stores are approved for that classification.
