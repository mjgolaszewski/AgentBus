# BCF 2.1 adoption measurements

This record separates the inherited AgentBus baseline from the final governed
result. Durations are observations, not assurance targets; a faster result is
acceptable only when it proves the same proposition.

## Inherited baseline

Source identity: `superworkspace-tools` commit
`aa8bba92e54b5d005c90c51d30556ab0858958b6`.

The source repository had no BCF installation, GitHub ruleset, environment, tag,
or release. Its single CI workflow ran `make check` and `make test` in parallel
Python 3.12 and 3.14 jobs. AgentBus shared those jobs with Weed, the installer,
and workspace maintenance tests.

| Observation | Baseline |
| --- | ---: |
| AgentBus tests | 61 |
| Local Python 3.14 elapsed time | 5.84 s |
| Workflow created to completed | 33 s |
| Provider queue before jobs started | 2 s |
| Python 3.12 job | 25 s |
| Python 3.14 job | 30 s |
| Combined job execution | 55 job-s |
| Combined test step in each job | 11 s |
| Unrelated shellcheck installation | 6–9 s per job |
| Jobs per direct-main change | 2 |

The workflow had no claim dependency model or authenticated evidence reuse. A
documentation-only change and a service protocol change both ran the same two
whole-repository jobs. Provider queue time was small and is kept separate from
execution time.

## Standalone characterization before governance

The preserved standalone application has 63 tests after adding operation
population closure checks. On the first standalone run:

| Observation | Result |
| --- | ---: |
| Python 3.12 tests | 63 passed in 7.22 s |
| Python 3.14 tests | 63 passed in 5.95 s |
| Cold Docker build | 17.58 s |
| Container import/user/data smoke | 1.82 s |

The HTTP timeout and redirect-safety cases deliberately account for most local
test time. They remain behavioral evidence rather than being shortened to make
the chart look better.

After adding the project-owned architecture propositions, the suite contains 70
tests. Its Python 3.12 run remains 5.8 seconds locally; lint and type checking
complete in under one second, while the network-backed vulnerability audit and
container smoke remain separate producers.

## Final result

The released BCF 2.1.4 wheel was verified at SHA-256
`a5aae2050e7ccb6e371758ab270d8d1ae5533d425def5e509cc50809a96fc1be`.
Upgrade installation passed strict validation, reconciliation converged after
one apply round, a second reconciliation was clean, and `bcf doctor` reported
Standard-v3 ready. AgentBus retains direct protected-main authority; it does
not adopt a trusted controller or BCF release authority.

The first reconciled prospective train observed the following local timings.
These measurements establish the final graph shape; the exact final candidate
is run again after this measurement record is committed.

| Stage | Wall clock |
| --- | ---: |
| Fixed-point normalization | 17.206 s |
| Planning, setup, and reuse decisions | 11.965 s |
| Behavioral producers and controls | 94.287 s |
| Terminal truth | 16.466 s |
| Locally knowable train | 139.924 s |

All 18 behavioral producers and all 18 isolated negative controls passed. Their
aggregate producer duration was 85.881 seconds; the longest single producer was
static type checking at 17.334 seconds. Independent work ran concurrently in
the canonical four-shard graph. The resulting local evidence bundle was
`bba62d03a22d320dd0a78bf7046683af1c49522bc4ee35920df54473b62b6d9e`.
Local receipts remain non-authoritative, so certification, merge eligibility,
and exact-main truth remain provider-owned.

BCF now derives the full path from semantic intent and exact commit/tree
identity through normalization, affected-claim planning, evidence fan-out,
negative controls, terminal truth, and provider-required lifecycle boundaries.
The agent-facing governance command surface is one prospective-train command;
agents no longer choose test roots, shard membership, gate order, evidence
artifacts, or lifecycle transitions.

## Provider proof and critical path

The protected GitHub path exercised the same generated graph as the local
train. The first complete pull-request run took 125 seconds from creation to
completion. Its preflight took 27 seconds, its four evidence shards took 43,
56, 46, and 40 seconds in parallel, and terminal truth took 31 seconds. The
exact corrected `main` candidate took 132 seconds end to end: 33 seconds of
preflight, evidence shards of 57, 44, 43, and 45 seconds, and 33 seconds of
terminal truth. Provider scheduling gaps on that final run totaled about nine
seconds; the BCF-controlled critical path was about 123 seconds.

One pull-request run took 305 seconds because the provider's artifact download
inside terminal truth stalled for 180 seconds. That wait is provider transport
latency, not BCF execution time, and is excluded from the controlled critical
path. Across the two successful pull-request runs, the deterministic failure,
and the corrected exact-main run, GitHub recorded 965 job-seconds.

The first merged candidate supplied no successor predicates for its workitem
chain. Cheap preflight rejected it in 23 seconds and skipped all four evidence
shards. The corrected canonical `requires-workitem-closure:` predicates then
passed both pull-request and exact-main truth. A trial with the noncanonical
`predecessors` field was rejected by schema validation before execution. These
are the representative early-rejection and fail-closed ambiguity proofs.

The claim planner distinguishes application-local, shared-contract, and
unrelated changes through the semantic ownership and dependency graph. In this
direct protected-main topology, however, ordinary pull-request and push events
do not receive provider-authenticated prior evidence. Local receipts are
deliberately ineligible. The planner therefore left all 18 applicable claims
unresolved and executed their producers in four parallel shards. No evidence
was reused because none was eligible. This is the minimum justified execution
for the selected no-controller authority model; installing trusted-controller
capability merely to obtain reuse would violate the adopter-owned boundary.

## Release and consumer cutover

AgentBus v0.3.0 was published from commit
`9ceb0cd4916291b5fb3f65b81eda4c84bce1ad53`, tree
`f33b9e7be7e484ded2f073945cc12dbef42b3319`. Its 3,133,999-byte source archive
has SHA-256
`d29b22066ccae21f54e46c9d85d1ddb53c996f670935e998333eb7a54e269e59`
and passed GitHub artifact attestation verification. The superworkspace now
consumes that archive through an exact lock containing archive, member, and
mode hashes. Its final main run passed on Python 3.12 and 3.14.

The live expand/contract cutover preserved the existing `.env`, SQLite inbox,
454 messages, maximum cursor 886, one claim, the inbox UUID, and all six local
persona profiles with their identities, chat IDs, and acknowledgement cursors.
The restarted v0.3.0 service passed SQLite integrity, Slack connectivity, and a
30-file drift audit. The embedded superworkspace source was removed only after
that proof. Release media and the rollback snapshot remain on the shared
network filesystem.

## Governance amplification

The standalone adoption used two pull requests, five feature or corrective
commits, two merge commits, four AgentBus workflow runs, 21 provider jobs, one
corrective intervention, and one immutable release. It used zero trusted
controller transitions. The consumer cutover used one pull request and six
provider jobs across feature, pull-request, and exact-main validation. The
normal agent-facing governance surface remains one prospective-train command;
BCF derives normalization, applicability, shard allocation, evidence, truth,
and eligibility from semantic intent plus exact commit and tree identity.
