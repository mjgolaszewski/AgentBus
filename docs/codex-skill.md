# AgentBus skill for Codex in VS Code

The release includes the self-contained `skills/agentbus` folder: `SKILL.md`,
Codex UI metadata, and a participation reference. It teaches the existing CLI
workflow; it is not a VSIX extension, service installer, credential bundle, or
wake adapter. The AgentBus service and CLI must already be configured.

From the extracted, verified AgentBus release, install for the user running
Codex's VS Code extension:

```bash
mkdir -p "$HOME/.agents/skills"
test ! -e "$HOME/.agents/skills/agentbus" && \
  cp -R skills/agentbus "$HOME/.agents/skills/agentbus"
```

If the destination already exists, compare it with the release and preserve any
local customizations before replacing it. To scope installation to one project,
copy the same folder to that project's `.agents/skills/agentbus` instead. In
Remote SSH or a dev container, use the filesystem where Codex executes; copying
to the desktop's unrelated home does not configure the remote workspace.

Open the skill list or mention `$agentbus` in Codex. If it is not discovered,
restart Codex's extension session; no AgentBus restart is needed. Installation
does not enroll a chat or change existing session, channel, wake, or host state.
Each new conversation still needs its own profile and Slack operator approval.

The current [official Codex skill documentation](https://learn.chatgpt.com/docs/build-skills)
describes local discovery and invocation. Older Codex installations may use
`~/.codex/skills`; check the installed client's skill list and documented search
paths instead of installing duplicate copies. VS Code discovery itself requires
a compatible Codex extension and is separate from archive/layout verification.

Live AHP/native-queue interoperability and atomic idle-only wake remain
unqualified. The skill preserves notification-only handling for unknown owners
and never grants permission to steal a conversation's writer lock. See
[Experimenters](audiences/experimenters.md) for qualification boundaries.
