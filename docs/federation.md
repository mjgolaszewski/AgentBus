# Federation construction boundary

AgentBus federation is an outbound provider bridge between independently
installed services. The local SQLite store records a bridge obligation before
any provider side effect. Provider submission, uncertainty, destination
persistence, and semantic agent acknowledgement remain separate states.

The deterministic foundation includes:

- idempotent bridge obligations keyed by origin instance, origin message, and
  destination instance;
- monotonic bridge transitions that cannot acknowledge before destination
  acceptance;
- a revisioned remote-directory projection with explicit withdrawal, stale,
  gap, and resynchronization states;
- service-owned registration projection that advertises only approved active
  registrations and emits withdrawal for stopped registrations;
- bounded snapshots, UTF-8 payloads, publisher bindings, and metadata
  allowlisting that reject credentials, privileges, execution, and wake
  authority fields.

`FederationStore` uses the existing AgentBus SQLite connection. It does not
create a broker, public endpoint, second registry, provider worker, session, or
execution capability. Provider submission is intentionally outside this local
store. Provider fan-out, origin authentication, and recovery remain P11
qualification gates and are not claimed by deterministic local primitives
alone. A configured publisher binding is required before a remote event can be
treated as originating from a namespace; discovery never bootstraps trust or
local execution authority.
