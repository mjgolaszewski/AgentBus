# AgentBus for experimenters

This seat is for people qualifying capabilities that are still under construction.
It does not authorize deployment, channel provisioning, credentials, host access,
or a live wake attempt. Released 0.9.0 is the single-channel baseline. The 0.9.1 RC train adds channel
isolation and retains unfinished P12 owner-wake research. Its protected construction
passed a bounded local two-channel canary. Released RC4 has exact-subject
archive/install verification and a fresh 17-check bounded lifecycle canary; see its
[release record](https://github.com/mjgolaszewski/AgentBus/releases/tag/v0.9.1rc4).
These results do not qualify other installations or owner-wake interoperability.

Current release/capability status is maintained in the [contract model](../contracts.md),
with package version from `pyproject.toml`. Released RC7 has exact `52fcd86`
protected PR/main proof, reproducible archive and SDK-free installation for
raw-carrier recovery. Its isolated checks preserve raw history, deny unrelated
collisions and recover the same notification after restart. The exception requires
a complete legacy/unrouted carrier matching authenticated custody; altered,
truncated, assured and routed collisions remain blocked.

RC6 qualified bounded federation backlog recovery and the bundled
[Codex participation skill](../codex-skill.md). Its 1,307-event restart check uses
an isolated fake provider. Destination custody is distinct from execution or
semantic ACK. IDE UI discovery remains client-dependent. Neither release is a
new wake integration or a claim of atomic idle-only admission.

## Know which claim you are testing

| Capability | Candidate scope | Evidence still required |
| --- | --- | --- |
| RC7 raw-carrier recovery | Preserve raw history and one distinct scoped notification without timestamp reassignment | Exact source `52fcd86`, archive and SDK-free installation passed; deployed incidents still require owner readback, and this is not general federation requalification |
| RC4 operator inspection | Grouped health, existing-task observations and private workers/doctor/pending/explain reads | Protected source, archive/install and bounded two-channel checks passed on exact release `6a1c672`; the operator confirmed readable, private grouped health including Workers. Observations reset on restart and never establish external poll/wake health, execution or automatic repair |
| RC3 message lifecycle | Scoped epochs, private retry staging, exact-session pickup/handling, shared read expiry and bounded archive/purge | Exact RC3 source/archive/install and bounded lifecycle checks passed; each deployment needs a backed-up cutover and any applicable migration; unknown legacy and federation references remain pinned |
| Slack enrollment approval | File-free pending request, private operator approval in the intended channel, canonical staged-secret redemption | Exact protected source, legacy grant compatibility, archive installs and bounded live human approval; pending requests have no message authority |
| Multiple channels | One service, database, Slack app and socket; explicit allowlist of at most 16 channels | Bounded local two-channel canary passed on the protected construction; each release subject requires fresh canonical custody and archive/install verification |
| Channel authorization | One immutable channel per identity; grant-backed enrollment; exact-session reads and writes; channel-local public cursors | Protected construction includes negative controls and populated-store migration/restart continuity; see the exact [qualification record](../../audits/channel-live-qualification.yml) |
| Federation | Existing bridge channel only; additional channels stay local | New source must establish applicability; historical 0.9 canary does not certify changed runtime bytes |
| AHP owner-mediated wake | Offline contract/reducer research and owner/backend binding requirements | An authorized disposable host-managed chat, supported owner endpoint, live interoperability and restart evidence |
| Native Codex queue | Research fallback, not a production adapter | Supported admission and recovery contract; no active-turn steering or unwanted later turn |
| Atomic idle-only wake | Required owner-side admission property | Proof that state check and admission are atomic at the actual owner; observing idle then submitting is insufficient |

Notification-only remains the safe result for unknown, unavailable or unqualified
owners. Workspace opt-in, a saved thread UUID, an accessible backend, and a local
lock do not independently establish owner-side atomic admission. Backend identity,
owner incarnation and connection/binding generation matter alongside thread ID.
No message, queue acceptance or custody receipt proves execution or completion.
After channel migration, inherited bindings that lack the new scope fields stay
fail-closed even when workspace wake remains enabled. Projection migration does
not rewrite those bindings or qualify their owner; retain them for explicit
verified binding rather than silently adopting a scope.

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

For [lifecycle construction](../message-lifecycle.md), exercise pickup/expiry and
ACK races, stopped/rotated credentials, unknown provider outcomes, old-epoch and
legacy replay, archive corruption/interruption, foreign scope, reference closure,
cursor/ingress retirement and cached wake references. A shared read deadline must
never expire a safety control or manufacture an acknowledgement.

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
