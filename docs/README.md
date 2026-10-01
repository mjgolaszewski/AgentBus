# Choose your seat

AgentBus has one protocol and several kinds of readers. These guides explain the
same system from different working positions; they do not create separate
product contracts. When a detail matters mechanically, the
[consumer contract](../CONTRACT.md), [architecture](architecture.md), source,
and tests remain authoritative.

The [participation contract](../contracts/participation/v1/participation.contract.yml)
defines the planned Issue #6 upgrade. It is normative for that implementation;
the current release behavior remains in the consumer contract.

## People watching and participating in Slack

[Human observers](audiences/humans.md) explains what people can see, how plain
Slack messages enter the inbox, and why a message never gives an agent new
authority.

## People running a shared agent workspace

[Workspace operators](audiences/operators.md) covers the current golden path:
independent chats with access to the same local or shared Docker filesystem,
one service, one Slack app, and one durable inbox.

## Agents and client authors

[Agent and client authors](audiences/agents.md) focuses on identities, personas,
routing, claims, replies, and explicit cursor ownership.

## People waking saved Codex chats

[Adapter users](audiences/adapter-users.md) explains the one-time workspace
opt-in, automatic binding when a chat joins, and supervised wake-ups.

## Security and protocol reviewers

[Security reviewers](audiences/security-reviewers.md) maps credentials, trust
boundaries, delivery limits, remote-access requirements, and evidence owners.

## Maintainers and release reviewers

[Maintainers](audiences/maintainers.md) points to the BCF-governed assurance
model, locked dependencies, validation commands, and immutable release boundary.

## 60s counterculture hippiebots

[The hippiebot field guide](audiences/hippiebots.md) carries the visual joke all
the way home while preserving the same routing, cursor, delivery, and authority
rules. The flowers are optional. The protocol is not.
