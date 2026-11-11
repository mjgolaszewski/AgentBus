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

### Install the client Slack app

A client receiving updates does not need its own AgentBus service or location.
Install a dedicated posting app in the client workspace; the existing AgentBus
location makes outbound Web API calls with that app's locally protected token.
Keep the coordination app and client posting app separate.

1. In [Slack app management](https://api.slack.com/apps), choose **Create New App**,
   **From a manifest**, then the client workspace. Import the
   [client manifest](examples/client-slack-manifest.json). Customize its name
   and branding if desired; request workspace-administrator approval as needed.
2. Install the app and invite its bot into the chosen updates channel, including
   for private channels. The manifest requests only `chat:write`; it does not
   request history, public-channel posting without membership, incoming events,
   slash commands or Socket Mode. No app-level token is needed.
3. Obtain the **Bot User OAuth Token** under **OAuth & Permissions**. Install it
   through the host custodian's protected credential procedure, never in Git,
   Slack messages or AgentBus messages. Store it in the service environment
   variable named by `token_env` below. There is no coordination-token fallback.
4. Verify the installed token with Slack's
   [`auth.test`](https://docs.slack.dev/reference/methods/auth.test). Use its
   `team_id` (`T…`) and `bot_id` (`B…`), not the App ID (`A…`) or bot-user
   `user_id` (`U…`). The custodian can retrieve these locally once the token is
   installed. Record the intended channel ID from Slack's channel details or
   copied channel link.
5. Select exact enrolled author routes and their coordination channels. Select
   approvers from the existing coordination service's human operator allowlist.
   Client-workspace IDs do not identify those operators in a different
   workspace; receiving client users need no AgentBus enrollment.

This connector exposes no `/agentbus` or `/ab` in the client workspace and does
not turn client replies into tasks. Adding slash commands to this manifest
without a supported client-side handler would not make them functional.

### Bind the destination and authors

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

For example, keep the actual token in a protected local service environment:

```dotenv
AGENTBUS_CLIENT_SLACK_BOT_TOKEN='REPLACE_LOCALLY_WITH_CLIENT_BOT_TOKEN'
AGENTBUS_PUBLICATIONS_CONFIG=/absolute/private/path/client-publications.json
```

These are service inputs; this is not a complete replacement for the existing
service environment. Workspace/bot/channel/author/operator IDs belong in the
JSON policy above, not invented environment switches. Do not add the client
channel to `AGENTBUS_SLACK_CHANNELS` or replace the coordination app's tokens.
The existing launcher must load the protected environment; coordinate the
backup/configuration/restart boundary with the custodian.

To skip per-message approval for one exact authorized author, set:

```json
{
  "authors": ["local:demo:writer"],
  "direct_authors": ["local:demo:writer"]
}
```

These are fields in the same destination object, not a standalone JSON document.
Keep `approvers` configured even when all current authors post directly.
Direct permission applies only to that destination and those exact authors.
New authors require another deliberate policy change; no agent self-enrollment
or federation trust can supply it.

### Verify one deliberate update

After activation, use the `update` command above for one labelled test. Inspect
the original `update-status` receipt and the actual destination post. With
approval required, review the exact draft through the coordination channel's
configured `<command> updates`; with direct policy, no approval click is needed.
Ordinary bus sends must produce no client posts. Confirm that human replies in
the client channel do not become AgentBus inbox work.

An uncertain receipt can coexist with a visible post. Preserve the original UUID
and report that discrepancy; do not post another copy to test delivery. The
[receipt-verification defect](https://github.com/mjgolaszewski/AgentBus/issues/120)
is tracked separately from this setup guidance. Human observation must not be
silently substituted for missing provider evidence in canonical custody.

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
