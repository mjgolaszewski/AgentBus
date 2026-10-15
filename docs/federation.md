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
- metadata validation that rejects credentials, privileges, execution, and wake
  authority fields.

`FederationStore` uses the existing AgentBus SQLite connection. It does not
create a broker, public endpoint, second registry, provider worker, session, or
execution capability. Provider fan-out, origin authentication, and recovery
remain P09/P11 qualification requirements and are not claimed by these local
primitives alone.
