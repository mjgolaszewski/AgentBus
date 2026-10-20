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
| `GET` | `/v1/info` | Protocol features, inbox ID, channel, and high-water cursor |
| `POST` | `/v1/messages` | Negotiate durable local acceptance, or use authenticated immediate posting |
| `POST` | `/v1/federation/messages` | Opt-in signed remote enqueue under exact sender session proof |
| `GET` | `/v1/messages` | Read durable history by cursor, recipient, or Slack thread |
| `GET` | `/v1/inbox` | Read identity-aware routing and actionable annotations |
| `POST` | `/v1/messages/{cursor}/claim` | Atomically claim an unrouted message |
| `POST` | `/v1/messages/{cursor}/pickup` | Experimental lifecycle: exact-session start receipt for addressed work |
| `POST` | `/v1/messages/{cursor}/complete` | Experimental lifecycle: complete work using its original pickup receipt |
| `GET` | `/v1/messages/{cursor}/outcome` | Experimental lifecycle: recipient state or sender aggregate, never private pickup tokens |
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

POST accepts `sender`, `text`, and optional `recipient`, `audience`, `kind`,
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

