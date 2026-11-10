# Deliberate client updates

Let the crew talk freely on the bus, then send a considered update to a named
client destination. This opt-in feature requires a matching qualified 0.11 client
and service. It supplies controlled Slack publishing, not client task intake or
a promise that an agent's wording is suitable for a particular audience.

## Author an update

```bash
agentbus update --identity local:demo:writer --destination project-updates 'The build is ready for review.'
```

A positional `-` reads at most 6000 characters from stdin. The destination is an
operator-configured name. There is no recipient, location, federation, workspace
or channel switch. Joining the bus grants no publishing permission.

The returned original request UUID identifies this immutable draft. Inspect your
own recent drafts with `agentbus update-status --identity ROUTE`; add
`--message-id ORIGINAL_UUID` for one receipt. A stopped exact session may inspect
its receipts but cannot create or cancel an update. Other sessions cannot borrow
that receipt or reuse the original UUID for the same chat.

Default flow:

```text
draft → awaiting approval → ready → submitted / uncertain → delivered / failed
```

Use `agentbus update-cancel --identity ROUTE --message-id ORIGINAL_UUID` only
before submission starts. Uncertain or delivered publication cannot be reported
cancelled. A cancelled identity stays reserved; editing means a new deliberate
draft and new approval, not an altered retry.

## Approve exactly what will be posted

In the author's coordination channel, an allowlisted destination approver uses
`<command> updates` and private buttons. Choose **Review exact update** from the
bounded list, inspect the complete retained rendering, then use **Approve exact
update**. The review card identifies the author, named
destination, workspace/channel binding, policy revision and exact retained
rendered text. Approval binds that draft's digest; another actor, destination,
content or policy revision cannot substitute for it.

Approval lasts within the draft's 24-hour lifetime. Authorization is checked
again immediately before submission. An operator may explicitly list selected
identities in one destination's `direct_authors`; only those authors skip human
approval for that destination. Enrollment and ordinary federation trust do not
supply this policy. Repeated clicks return the retained outcome without posting
again. Failed private confirmation calls for inspecting results, not a new draft.

## Configure the outbound installation locally

Keep a private configuration file outside Git and point
`AGENTBUS_PUBLICATIONS_CONFIG` at its absolute path. Example identifiers below
are placeholders to replace with the operator-approved installation and channels:

```json
{
  "version": 1,
  "installations": [
    {"name": "client", "team_id": "TCLIENT123", "bot_id": "BCLIENT123", "token_env": "AGENTBUS_CLIENT_SLACK_BOT_TOKEN"}
  ],
  "destinations": [
    {
      "name": "project-updates", "installation": "client", "channel": "CCLIENT123",
      "source_channels": ["CWORK123"], "authors": ["local:demo:writer"],
      "approvers": ["UOPERATOR123"], "direct_authors": [],
      "enabled": true, "label": "Project update"
    }
  ]
}
```

Each location needs its own explicitly installed credential for every configured
client Slack installation. `token_env` names the local environment variable; the
configuration contains no token. There is no implicit coordination-token fallback
or credential replication through federation. The provider's actual workspace
and bot must match the configured binding before a post.

Use a client channel outside this service's coordination allowlist. A destination
that overlaps the same coordination workspace/channel is rejected. Client
messages and replies are outside inbound subscriptions and never become bus
tasks. Ordinary sends, broadcasts, polls, receipts and signed federation frames
create zero client-publication obligations.

One existing service/database/worker and HTTP client handle custody and outbound
Web API calls. There is no additional Socket Mode connection or listener. At most
one publication provider call uses one slot in the existing aggregate cycle
budget. Verification happens only for a pending publication; disabled or idle
publishing adds no periodic provider probes. Limits are 16 installations,
16 destinations, 1024 retained publications and 6000 text characters per draft.
There is no automatic publication-receipt pruning; capacity exhaustion rejects
new drafts for owner review rather than deleting uncertain or accepted custody.

## Recover honestly

The private publication stage is separate from message-send staging and binds
the service, chat, session, channel, inbox, original UUID and exact payload.
For an uncertain admission response, inspect the original receipt. The identical
`update` with `--message-id ORIGINAL_UUID` requires that existing stage and keeps
its original epoch; a missing stage, changed payload or foreign scope fails
closed. A fresh UUID is not an uncertain-delivery recovery strategy.

A provider timeout or unverified post response remains uncertain and is not
blindly retried. An explicit rate rejection preserves the same payload/identity
and honors durable per-installation/method backoff. `delivered` means the bound
provider confirmed the post, not that a client read it or accepted its claims.

Back up the existing database consistently before enabling the feature. After
new custody is accepted, retain the new binary/database and repair forward;
disabling publishing preserves drafts and uncertainty. Lifecycle-enabled stores
use an internal startup guard that older releases reject; non-lifecycle stores
still require the operator to enforce the repair-forward rule.

AgentBus assumes a trusted workspace and non-hostile participants. It cannot
prevent an authorized author from making an inappropriate statement or inferring
permission from a message. See [security](../SECURITY.md) and the
[Experimenters seat](audiences/experimenters.md) for the qualification boundary.
