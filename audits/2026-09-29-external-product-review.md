# External product review disposition

An external source review identified five material concerns in AgentBus 0.3.1:

1. Slack events containing a syntactically valid AgentBus envelope were routed
   without proving that the configured AgentBus bot produced the event.
2. Logical agent identities remain self-asserted under one shared API token.
3. The embedded BCF assurance surface is large relative to the application.
4. Consumer state and project discovery retained superworkspace-specific defaults.
5. Identity-aware inbox reads filtered the complete post-cursor tail in memory.

The unauthenticated Slack envelope is a high-severity provenance defect. P03
binds routed envelopes to the bot ID returned by Slack `auth.test`; human and
foreign-bot envelopes remain unrouted. Contract tests preserve the reported
human-spoof negative control and add foreign-bot and startup-failure controls.

P03 also bounds inbox reads in SQL, migrates the routing audience for existing
SQLite databases, adopts an XDG consumer-state default with a legacy compatibility
fallback, makes the default supported-interpreter suite valid, and documents
Slack retention.

Per-agent credentials, signed federation, clustering, multi-tenant authorization,
and changes to BCF distribution remain deferred because the current product is a
single trusted-workspace service. BCF is excluded from the runtime container;
its source footprint will change only through supported BCF distribution
machinery.
