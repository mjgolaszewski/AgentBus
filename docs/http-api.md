# AgentBus HTTP API

All `/v1` routes require an authorized bearer; operator routes use the separate
operator capability. Agent data-plane routes additionally require
`X-AgentBus-Session-Token` and `X-AgentBus-Inbox-ID`. Authenticated info bootstrap
may omit the inbox assertion; enrollment consumes an operator grant and stages
its session secret. No agent-supplied channel selector is accepted.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Public liveness only |
| `GET` | `/v1/status` | Slack connectivity; aggregate footprint only with operator capability |
| `GET` | `/v1/search` | Literal retained-text search with exact session scope and opaque continuation |
| `POST` | `/v1/publications` | Opt-in explicit named-destination draft admission under original UUID and publication epoch |
| `GET` | `/v1/publications`, `/v1/publications/{request_id}` | Author-session-only publication receipts, including exact stopped-session recovery |
| `POST` | `/v1/publications/{request_id}/cancel` | Active author cancels only before provider submission starts |
| `POST` | `/a2a` | Opt-in authenticated loopback A2A 1.0 subset; separate protocol negotiation required |
| `GET` | `/v1/info` | Protocol features, inbox ID, channel, and high-water cursor |
| `POST` | `/v1/messages` | Negotiate durable local acceptance, or use authenticated immediate posting |
| `POST` | `/v1/federation/messages` | Opt-in signed remote enqueue under exact sender session proof |
| `GET` | `/v1/messages` | Read durable history by cursor, recipient, or Slack thread |
| `GET` | `/v1/inbox` | Read identity-aware routing and actionable annotations |
| `POST` | `/v1/messages/{cursor}/claim` | Atomically claim an unrouted message |
| `POST` | `/v1/messages/{cursor}/pickup` | Experimental lifecycle: exact-session start receipt for addressed work |
| `POST` | `/v1/messages/{cursor}/complete` | Experimental lifecycle: complete work using its original pickup receipt |
| `GET` | `/v1/messages/{cursor}/outcome` | Experimental lifecycle: recipient state or sender aggregate, never private pickup tokens |
| `GET` | `/v1/requests/{request_id}/outcome` | Exact-session authored request lookup; retained transport facts, missing evidence stays unknown |
| `POST` | `/v1/receipts/handled` | Experimental lifecycle: bounded explicit handling receipts before local ACK cleanup |
| `POST` | `/v1/sessions/enrollment-grants` | Operator: grant one route/repository immutable channel membership |
| `POST` | `/v1/sessions` | Consume grant; receive session, channel/inbox scope and policy revision |
| `GET` | `/v1/sessions/{id}/self-presence` | Read only the proved session's current presence without an operator token |
| `POST` | `/v1/sessions/{id}/policy-ack`, `/check-in` | Acknowledge policy and check for controls under session proof |
| `GET` | `/v1/sessions/roster`, `/v1/sessions/{id}/presence` | Operator-only joined-session view |
| `POST` | `/v1/policy/revisions`, `/v1/controls/stop`, `/v1/controls` | Operator-only policy and control changes |
| `POST` | `/v1/controls/{id}/targets/{session_id}/ack` | Exact-session control receipt |
| `POST` | `/v1/sessions/{id}/rename`, `/rotate-secret` | Session-preserving identity and credential changes |

Operator control requests accept either `routes`, `session_ids`, or
`all_current`; exactly one target mode is required. Session IDs avoid ambiguity
when multiple chats work in the same repository.

Message POST accepts `sender`, `text`, and optional `recipient`, `audience`, `kind`,
`repo`, `correlation_id`, `thread_ts`, and `reply_to_cursor`. Clients cannot
choose another Slack channel, supply a bot token, or assert their own assurance
label. A send cannot use `slack:` as its sender or an unknown Slack thread
parent. Message text is capped at 6,000 characters, the HTTP request body at
64 KiB, and session credentials at 512 characters. The encoded Slack envelope
has a separate safety bound.

Message reads return:

```json
{"messages": [], "next_cursor": 42, "has_more": false}
```

Save `next_cursor` per consumer and filter. Continue while `has_more` is true.
Public cursors are monotonic integers local to one channel inbox. Neither their
numbers nor high-water marks reveal other channels' activity. Slack timestamps
remain strings and are resolved only in the authenticated channel.


## Explicit publication and local adapter bounds

Publication admission uses `Idempotency-Key` for the original author UUID and
`X-AgentBus-Request-Epoch` for its privately staged publication epoch. Info exposes
`client-publication-v1` and the channel-bound publication epoch only when enabled.
JSON accepts only `destination` and `text`; there is no arbitrary channel or URL.
A matching 202 receipt confirms admission, not approval, provider delivery or read.
Own receipt listing defaults to 20 and is bounded to 20 records per page.
See [client publishing](client-publishing.md) for exact approval and retry rules.

The [local A2A adapter](local-adapters.md) requires `A2A-Version: 1.0` and the
normal exact-session/inbox proof. Its task completion means origin durable
mailbox admission only. Streaming/push and public A2A deployment are unsupported.
These opt-in interfaces grant no host execution or operator capability.

A stopped exact session can query its own authored and recipient outcomes without
ordinary information, history or send authority. Missing, revoked or foreign
scope still fails closed. Newly retired authored UUIDs report retained acceptance
with `history: retired`; unknown legacy retirement remains unknown.

A non-authorizing ASGI output observer records actual successful session API
access at most once per 30 seconds. `/healthz`, minimal status, failed proof and
unproved operator inspection do not refresh it. Self-presence exposes only the
proved session's latest observation and explicit service gap; model work and
continuous inbox monitoring remain unobserved.
