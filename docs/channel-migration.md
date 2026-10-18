# Channel migration for 0.9.1 RC

This is an offline procedure for the 0.9.1 release candidate. A bounded local
two-channel migration/restart canary passed on its protected construction.
Deployment remains a separate custodian action requiring a verified backup. Released 0.9.0 must not open
a migrated database. Live multichannel readiness additionally needs final
canonical proof, archive installation and a bounded two-channel Slack canary;
see [Experimenters](audiences/experimenters.md).

## Prepare the original channel

The operator identifies the existing Slack channel ID, stops the service, and
verifies a restorable backup of SQLite and consumer profiles. Account for WAL
state when making the backup. Do not copy an active database with plain file
copy. Preserve pending obligations, observations, claims, alias history,
credentials, ACK positions, inbox UUID and federation mappings.

Configure either `AGENTBUS_SLACK_CHANNEL=CHANNEL_ID` or
`AGENTBUS_SLACK_CHANNELS=CHANNEL_ID,SECOND_CHANNEL_ID`, never both. The allowlist
contains 1–16 unique Slack channel IDs. A removed channel keeps its data and
obligations; its message access/posting pauses. It cannot transfer membership.
The existing federation channel must remain explicitly configured; extra
channels are local-only and cannot export their identities to its directory.

## Inspect before applying

Use the candidate CLI with the intended database path configured locally:

```bash
agentbus migrate-channels --legacy-channel C0123456789 \
  --channels C0123456789,C0987654321
# After reviewing the dry-run and verified backup:
agentbus migrate-channels --legacy-channel C0123456789 \
  --channels C0123456789,C0987654321 --apply
```

The command takes an exclusive service lease and refuses unexplained existing
channels, ambiguous membership and conflicting credentials. Dry-run does not
change database content. Apply commits the additive schema, bindings and public
positions together. It preserves existing internal custody IDs, old public
cursor values and the original inbox UUID. A new channel has its own stable
inbox and independent public position sequence. Startup refuses a populated
legacy database that has not completed this explicit migration.

## Update clients without moving membership

Install the matching client. It requires advertised session and channel-local
cursor capabilities and rejects unsupported servers. Existing joined profiles
must authenticate the same chat, session, service, channel and inbox. Disable and drain the workspace wake worker and stop the profile
poll workers, then run `agentbus migrate-profile-scope --identity
PREFIX:REPO:NAME` to validate and rebuild legacy disposable projections. The
migration keeps private backups and resumes from the existing ACK position;
it does not acknowledge pending work. Mismatched state is evidence to retain,
not a reason to rebind to another channel. Revalidate any host binding separately;
uncertain wake attempts must be reconciled before replacement.
Legacy bindings missing the new service/inbox/channel scope remain fail-closed.
Preserving workspace enablement does not upgrade those bindings or establish an
owning-host endpoint; preserve their bytes until explicit verified binding.

The RC2 approval construction lets an eligible unjoined profile request Slack
approval with `join --wait-for-approval`; the allowlisted operator approves the
request UUID in the intended channel. It retains the local UUID, and pending
requests have no message authority. This flow requires its own exact-source and
live qualification. For file-based enrollment, issue a grant with
`agentbus issue-enrollment --identity PREFIX:REPO:NAME --channel CHANNEL_ID
--output PRIVATE_FILE`, then use `join --enrollment-file PRIVATE_FILE` and
acknowledge the delivered policy. Use `issue-handoff` only when explicitly
preserving an eligible unjoined profile UUID. Neither operation moves an
enrolled identity. Delete grant files after successful enrollment.

## Validate and recover

First validate the original channel: health/connectivity, exact-session info,
known history/claims/aliases/ACK continuity, pending obligations, and a read-only
wake status. Enable no live wake for an unqualified owner. Introduce a second
isolated channel only within an approved canary window, using separately granted
identities and explicit cross-channel negative controls.

Before accepting new writes, rollback may restore the verified backup. After
new custody is accepted, retain the new binary and latest database; disable
additional channels if needed and repair forward. Never restore an old snapshot
over accepted obligations or run 0.9 against the migrated database. Live
restarts, configuration and publication remain custodian actions.
