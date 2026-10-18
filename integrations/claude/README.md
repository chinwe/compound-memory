# compound-memory (Claude Code plugin)

Local multi-agent shared memory for Claude Code. This plugin is a thin stdio wrapper around the published PyPI package `compound-memory`. It does not reimplement the five MCP tools and does not add an in-process LLM.

Requires [Claude Code](https://code.claude.com/docs) and [`uv`](https://docs.astral.sh/uv/) (`uvx` on `PATH`).

## Install

```bash
claude plugin marketplace add chinwe/compound-memory
claude plugin install compound-memory@compound-memory
```

One-liner, if your Claude Code build accepts it:

```bash
claude plugin install compound-memory --marketplace chinwe/compound-memory
```

The plugin sets `COMPOUND_MEMORY_AGENT_ID=agent-claude`. Override that env var in the host if you need a different id. Initialize the store once:

```bash
uvx --from compound-memory compound-memory init
```

Anthropic's official plugin Directory submission is out of scope. This is the self-hosted marketplace in this repository (`integrations/claude`). Those files are not in the PyPI sdist or wheel.

The short skill shipped with this plugin is [`skills/compound-memory/SKILL.md`](skills/compound-memory/SKILL.md). The canonical long-form skill is [`../../skills/compound-memory/SKILL.md`](../../skills/compound-memory/SKILL.md). The skill is the on-demand source for when and how. This directory has no `CLAUDE.md`: a plugin-root `CLAUDE.md` is not loaded as project context, and `claude plugin validate` warns on it.

## Session start

On a new session, `hooks/hooks.json` runs `hooks/session-start.sh` (`SessionStart`, matcher `startup`). The script prints a few hard rules as `hookSpecificOutput.additionalContext`. It does not read the memory store and does not call a model. Resume, `/clear`, and compaction do not re-run it.

Some Claude Code versions run a plugin SessionStart hook and then drop `additionalContext` ([anthropics/claude-code#16538](https://github.com/anthropics/claude-code/issues/16538)). After you install or update, start a new session and confirm the reminder is in context. `/hooks` lists this command. If it is missing, paste the snippet below into your own project `CLAUDE.md`.

Claude Code has no switch for a single hook:

- One run, every hook: `claude --settings '{"disableAllHooks": true}'`
- Every hook until you turn them back on: `"disableAllHooks": true` in `~/.claude/settings.json`. A project `.claude/settings.json` value of `false` overrides that.
- This plugin only (MCP server, skill, and hook together): `claude plugin disable compound-memory@compound-memory`

Optional snippet for your project `CLAUDE.md` (skip it when the hook reminder already appears):

```markdown
compound-memory: COMPOUND_MEMORY_AGENT_ID defaults to agent-claude in .mcp.json (overridable); source and agent use that same id. memory_search comes before non-trivial work; memory_feedback after an adopted hit is what compounds the store. Work inside a repo fits project-scoped memories. Five MCP tools; this plugin does not reimplement them.
```

## 安装（简体中文）

需要已安装 Claude Code，且 `uv` / `uvx` 在 `PATH` 上。

```bash
claude plugin marketplace add chinwe/compound-memory
claude plugin install compound-memory@compound-memory
```

插件默认 `COMPOUND_MEMORY_AGENT_ID=agent-claude`，宿主可以覆盖。记忆库初始化：`uvx --from compound-memory compound-memory init`。不提交 Anthropic 官方 Directory。

新会话会跑 SessionStart hook（matcher `startup`），注入上面英文片段里的几条硬规则。技能仍是按需查阅的用法来源。

部分 Claude Code 版本会静默丢掉插件 SessionStart 的 `additionalContext`（anthropics/claude-code#16538）。装好后开一次新会话，确认提醒出现；`/hooks` 能看到这条命令。看不到时，把上一节片段贴进你自己的项目 `CLAUDE.md`。

Claude Code 不能单关这一条 hook。一次会话关掉全部 hook：`claude --settings '{"disableAllHooks": true}'`。持续关闭全部 hook：在 `~/.claude/settings.json` 里设 `"disableAllHooks": true`（项目级同名 `false` 会盖掉它）。只停用本插件：`claude plugin disable compound-memory@compound-memory`。
