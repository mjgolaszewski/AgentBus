# Local mailbox tools

For copyable setup and first-request instructions, take the
[MCP and local A2A seat](audiences/mailbox-tool-users.md). This page records
the protocol boundaries and limits.

Search and MCP are available in 0.10.0 with a qualified matching release. They reuse
the normal authenticated AgentBus API and one bound profile. They grant no
operator capability, channel switch, provider credential or host wake authority.

## Search

```bash
agentbus search --identity ROUTE --query 'release ready' --limit 20
```

Search literal terms in retained text within the authenticated channel. Results
contain public inbox positions, never global database IDs or foreign counts.
The default page has 20 results, maximum 100. Pass the returned opaque `after`
value to the next query unchanged; it is bound to the exact query and session.
Use existing inbox thread/context options for related messages. Reads do not ACK.

The additive FTS5 migration verifies SQLite support before changing the database.
The index follows message insert/update/retirement transactions and can contain
sensitive message text just like the existing database. Include it in the same
private consistent backup. Keep service state on approved persistent storage.

## MCP

```bash
agentbus mcp --identity ROUTE
```

Configure a local MCP-capable client to launch this command over stdio. The
adapter pins protocol 2025-11-25. It exposes inbox, search, thread context,
check-in, send, reply, message status, pickup, completion and explicit semantic
acknowledgement. No tool accepts another identity, service URL or channel.

The client uses its existing private enrollment. Do not paste session secrets
into tool arguments or copy another profile. Identity/channel/inbox mismatch
stops the operation instead of automatically rebinding. Standard output is
protocol-only; idle waiting starts no model turn or independent poll worker.

Mutation receipts remain receipts. A failed send preserves the original staged
UUID where available; absent or uncertain status is not permission for a fresh
replacement. No tool automatically acknowledges presentation or completed work.

## Local A2A mailbox adapter

A matching qualified 0.11 service can opt in with `AGENTBUS_A2A_ENABLED=1`.
It uses `/a2a` on the existing loopback-bound service; no additional listener,
provider connection, discovery service or execution worker is created. Wildcard
or non-loopback configuration is rejected. The standard container/Compose
listener uses a wildcard address and cannot enable this adapter; a deliberately
loopback-bound deployment is required. Custom ASGI launchers must honor the
declared host and port. Actual client/server addresses and
the configured port must also be loopback-bound; a forged Host header supplies
no endpoint authority.

The adapter pins [A2A 1.0 JSON-RPC](https://a2a-protocol.org/v1.0.0/specification/)
and advertises a deliberately narrow `mailbox.accept` skill. Its authenticated
`GetExtendedAgentCard`, `SendMessage`, `GetTask`, `ListTasks` and `CancelTask`
operations require the existing bearer, exact session proof and bound inbox.
Unauthenticated discovery exposes no roster or private configuration.

Every RPC uses `A2A-Version: 1.0` and the normal session/inbox headers. A lifecycle
deployment also requires `X-AgentBus-Lifecycle-Version: 1`; first submission
supplies the original `X-AgentBus-Request-Epoch` obtained through authenticated
`/v1/info`. Do not substitute a new epoch for uncertain admission.

Example `SendMessage` parameters:

```json
{
  "message": {
    "messageId": "11111111-1111-4111-8111-111111111111",
    "role": "ROLE_USER",
    "parts": [{"text": "Please review the build when ready."}],
    "metadata": {"operation": "mailbox.accept", "recipient": "local:demo:builder"}
  }
}
```

Use a newly generated original UUID for a new intent; retain it for exact retries.
Only one text part, at most 6000 characters, is accepted. A server-generated task
ID is separate from that original message identity. A changed duplicate fails;
unknown and foreign task lookups share the same denial. Listing defaults to 20,
maximum 100, with scope-bound opaque continuation and no cross-channel population.
This local profile accepts only integer-zero history length and string pagination
tokens; omit unused settings. Empty output-mode preferences use the declared JSON
default. The adapter retains at most 4096 task-index records. Capacity exhaustion
rejects new tasks without pruning accepted identities; exact retained retries and
lookups remain available.

**Completed means origin durable mailbox admission.** This named operation can
therefore return a terminal task even with normal blocking configuration. Its
structured artifact keeps retained transport facts, recipient read/work and
semantic ACK separate. It never claims that a recipient model executed a task.
Accepted delivery cannot be revoked; `CancelTask` reports not-cancellable rather
than inventing success. Retired transport history remains an explicit observation.

Streaming, push notifications, file/URL parts, nonzero message history, arbitrary
metadata, autonomous execution and public A2A deployment are unsupported. This
local subset is not blanket interoperability with every A2A client. Use an
isolated client to qualify its exact protocol/binding expectations.

## Qualification limits

[Client publishing](client-publishing.md) is a separate explicit outbound operation.
No public listener, external discovery, client reply ingestion, production
AHP/native-queue adapter or atomic idle-only admission is established here.
AgentBus assumes a trusted workspace with trusted, non-hostile agents. Messages
supply no new authority, but cannot prevent an agent independently inferring it.
