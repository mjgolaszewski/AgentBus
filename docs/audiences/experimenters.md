# AgentBus for experimenters

This seat is for people qualifying capabilities that are still under construction.
It does not authorize deployment, channel provisioning, credentials, host access,
or a live wake attempt. Released 0.9.0 is the single-channel baseline. The P12
candidate preserves that lineage while adding channel isolation and retaining
unfinished owner-wake research.

## Know which claim you are testing

| Capability | Candidate scope | Evidence still required |
| --- | --- | --- |
| Multiple channels | One service, database, Slack app and socket; explicit allowlist of at most 16 channels | Final protected candidate/exact-main proof, archive/install verification, and a bounded two-channel Slack canary |
| Channel authorization | One immutable channel per identity; grant-backed enrollment; exact-session reads and writes; channel-local public cursors | Complete negative controls, populated-store migration/recovery proof, final canonical test population |
| Federation | Existing bridge channel only; additional channels stay local | New source must establish applicability; historical 0.9 canary does not certify changed runtime bytes |
| AHP owner-mediated wake | Offline contract/reducer research and owner/backend binding requirements | An authorized disposable host-managed chat, supported owner endpoint, live interoperability and restart evidence |
| Native Codex queue | Research fallback, not a production adapter | Supported admission and recovery contract; no active-turn steering or unwanted later turn |
| Atomic idle-only wake | Required owner-side admission property | Proof that state check and admission are atomic at the actual owner; observing idle then submitting is insufficient |

Notification-only remains the safe result for unknown, unavailable or unqualified
owners. Workspace opt-in, a saved thread UUID, an accessible backend, and a local
lock do not independently establish owner-side atomic admission. Backend identity,
owner incarnation and connection/binding generation matter alongside thread ID.
No message, queue acceptance or custody receipt proves execution or completion.

## Work offline first

Use disposable trees, fake provider transports and isolated database copies.
Read the [contract model](../contracts.md), [channel migration](../channel-migration.md)
and [architecture](../architecture.md). Keep local state outside Git. Use the
canonical BCF graph for final proof selection and custody; development pytest
results are diagnostics until applicability is authenticated.

For wake, exercise active-turn steering rejection, duplicate submissions, lost
responses, interrupted/unloaded/stale owners, backend mismatch, server restart
during queued submission, and restored permission configuration. An unknown write
outcome stays unresolved until its operation owner supplies evidence; reconnecting
or retrying blindly cannot decide whether the first submission executed.

For channels, exercise overlapping public cursor numbers, identical timestamps
and request IDs, foreign inbox assertions, stopped/rotated credentials, grant
replay and lost responses, aliases, reply ancestry, disabled channels, outages,
provider echoes, federation containment, and profile/spool/wake scope mismatch.
No negative control may acquire another channel by editing local profile fields.

## Move to a bounded live experiment

First prepare a reviewable offline candidate. The custodian must then identify
the channels, identities, isolated host/test chat where relevant, deployment
window, retention assumptions, stop conditions and rollback procedure. Keep
credentials installed locally. Live canaries use separately granted identities
and no production task authority. Coordinate restarts with the runtime owner.
This repository's tests and this guide grant none of those prerequisites.

Record the exact source/tree, configuration identities without secrets, observed
provider/host references, acceptance versus completion, failures, recovery, and
limitations. A failed or ambiguous case remains visible after a later success.
Historical provider health is not a guarantee of indefinite recovery or retention.

## Report the boundary honestly

Call a result an offline model, deterministic test, bounded live canary, or
release proof according to the evidence it actually carries. Never promote
"queue accepted" to "turn completed" or a past single-channel canary to current
multichannel readiness. Keep Issue #75 and unfinished qualification open until
their own acceptance criteria are proved. Runtime deployment and publication
remain separate custodian actions.
