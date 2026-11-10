# Operator federation

This guide describes the construction target for 0.10.0. Use these additions only
with a qualified matching client/service release and approved peer configuration.
Existing ordinary messages and local commands retain their current scope.

## Locations, channels and destinations

A location is an AgentBus installation. A channel is a Slack conversation and an
identity's immutable membership. A publishing destination is a separately
configured client workspace/channel binding; it is not another location.

Operator federation remains on the existing federation channel. It does not
forward other channels or change enrollment. Agents continue reading their
session-scoped local API; they do not acquire Slack credentials or remote
operator authority.

## Commands

Replace `<command>` with the configured primary name or an explicitly configured
alias accepted by the installed release. Both route to the same Slack app and
AgentBus installation; an alias is not a location selector.

```text
<command> health
<command> health --location LOCATION
<command> health --federated
<command> broadcast TEXT
<command> broadcast --location LOCATION TEXT
<command> broadcast --federated TEXT
<command> results
```

The selectors are mutually exclusive and supported only for health and
broadcast. Federation includes the receiving location and its approved peers;
one-location selection targets only that approved installation. Exact configured
instance names and unique configured route prefixes resolve to retained origin
identities. Unknown and unsupported peers remain visible, never silent success.

Options precede text. Use `--` before literal text beginning with a switch:

```text
<command> broadcast -- --federated is literal text here
```

A federated request returns privately without waiting for provider convergence.
Use Refresh and page buttons for per-location results. The normal Results view
also links your recent federated requests. Sample timestamps and stale/unknown
outcomes are explicit. Read acknowledgement never proves model execution.

## Explicit peer authority

Each participating installation must approve the new wire capability on the
existing signer/publisher binding (`operator_version: 4`). The destination also
requires `operator_controls` with version 1 and a bounded grants list. Each grant
binds the exact origin, human Slack actor, source channel, destination channel and
allowed `health`/`broadcast` operations. Only the existing federation channel is
supported. Both locations must be provisioned by their custodians; a shared Slack
channel or valid signed ordinary message grants no operator authority.

The operator grant configuration contains no agent session credential. Keep
signers and provider tokens locally protected. Revoking a grant prevents new
admission; retained accepted operations remain inspectable under their original
identities. Old-origin history and unresolved obligations are not adopted or
quarantined by changing an active binding.

## Recovery and bounds

The request UUID, content and target snapshot survive repeated Slack delivery,
response loss and restart. Each destination persists one shared broadcast and
its original eligible sessions, including an empty snapshot. Later enrollment
does not expand the request. Provider submission, destination acceptance, shared
read receipts and recipient execution are distinct.

Aggregate requests are bounded to 64 locations and 1024 retained operations per
store. Capacity exhaustion fails closed and requires an operator retention
assessment; unresolved history is never discarded automatically. Existing bridge
retry and provider budgets remain authoritative. Health samples are observations,
not continuous monitoring. A live transport or enrolled session does not prove
an agent is actively polling.


## Provider visibility

Private results refer to the operator's Slack response, not end-to-end transport
confidentiality. Federation frames are signed, not encrypted. Approved channel
members may inspect retained provider carriers. Remote health therefore includes
only the authorized channel's sample and bounded runtime/federation observations,
not other channels' histories, global database size or credentials. Qualify the
provider audience before enabling remote operator grants.
