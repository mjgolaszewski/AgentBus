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

The final prospective train will append:

- preflight, evidence, truth, and certification critical paths;
- every producer's duration and observed parallelism;
- executed, partially reused, and fully reused claim groups;
- deterministic failures moved ahead of behavioral fan-out;
- provider queue and scheduling latency;
- the final workflow, job, and agent-facing command surface;
- semantic change amplification across commits, pull requests, jobs, controller
  transitions, compute, and operator intervention.

Final measurements are recorded only from the reconciled candidate bytes. A
governed-byte change invalidates affected numbers and conclusions.
