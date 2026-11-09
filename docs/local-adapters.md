# Local mailbox tools

These additions target 0.10.0 and require a qualified matching release. They reuse
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

## Qualification limits

A2A and client publishing belong to the separately qualified 0.11.0 milestone.
No public listener, external discovery, client reply ingestion, production
AHP/native-queue adapter or atomic idle-only admission is established here.
AgentBus assumes a trusted workspace with trusted, non-hostile agents. Messages
supply no new authority, but cannot prevent an agent independently inferring it.
