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

The migration changed one pull request and three finalization commits before
provider submission: two editorial commits and one released-runtime upgrade.
It adds one governed workflow with cheap preflight, four parallel evidence
shards, and terminal truth. No trusted-controller transition is introduced.
Provider queue and hosted execution are recorded separately after the protected
PR run because local evidence cannot substitute for those provider facts.
