# Contributing

Start with the consumer contract and preserve its routing, cursor, and delivery
semantics. Changes to a public command, route, envelope, profile, or persistence
format need an explicit compatibility account and behavioral tests.

Install from the lock and run the tests without live Slack credentials:

```bash
uv sync --locked --extra dev
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run --locked --extra dev pytest
```

Governed changes use the canonical prospective train:

```bash
bcf ci submit --repo-root . --intent workitem
```

Generated BCF workflows and locks are derived surfaces. Change their owning
contracts and reconcile rather than editing generated files directly. Never
commit `.env`, `.state`, consumer profiles, inbox data, logs, or credentials.
