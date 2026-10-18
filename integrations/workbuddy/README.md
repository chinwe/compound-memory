# compound-memory (WorkBuddy / CodeBuddy plugin)

Local multi-agent shared memory for WorkBuddy and CodeBuddy. This plugin is a thin stdio wrapper around the published PyPI package `compound-memory`. It does not reimplement the five MCP tools and does not add an in-process LLM.

Requires [WorkBuddy](https://www.workbuddy.ai/docs/cli/plugin-marketplaces) or [CodeBuddy](https://www.codebuddy.ai/docs/cli/plugin-marketplaces) and [`uv`](https://docs.astral.sh/uv/) (`uvx` on `PATH`).

## Install

Inside the host:

```text
/plugin marketplace add chinwe/compound-memory
/plugin install compound-memory@compound-memory
```

The same commands work as `codebuddy plugin marketplace add chinwe/compound-memory` and `codebuddy plugin install compound-memory@compound-memory`.

The plugin sets `COMPOUND_MEMORY_AGENT_ID=agent-workbuddy`. Override that env var in the host if you need a different id. Initialize the store once:

```bash
uvx --from compound-memory compound-memory init
```

This is the self-hosted marketplace in this repository (`.codebuddy-plugin/marketplace.json`, plugin root `integrations/workbuddy`). Those files are not in the PyPI sdist or wheel. Hand-written MCP config at `~/.workbuddy/mcp.json` still works; see `docs/agent-integration.md` §2.

The short skill shipped with this plugin is [`skills/compound-memory/SKILL.md`](skills/compound-memory/SKILL.md). The canonical long-form skill is [`../../skills/compound-memory/SKILL.md`](../../skills/compound-memory/SKILL.md).

## Session start

On a new session, `hooks/hooks.json` runs `hooks/session-start.sh` (`SessionStart`, matcher `startup`). The script prints a few hard rules as `hookSpecificOutput.additionalContext`. It does not read the memory store and does not call a model. Resume, `/clear`, and compaction do not re-run it. Search before non-trivial work is still required: this hook does not recall memories.

If the reminder is missing after a new session, paste the snippet below into the project `CODEBUDDY.md`. When that file is absent, WorkBuddy and CodeBuddy load `AGENTS.md` instead. User-level memory for the hand-written path is `~/.workbuddy/MEMORY.md`.

Turn the hook off:

- One setting, every hook: `"disableAllHooks": true` in `~/.codebuddy/settings.json`.
- This plugin only (MCP server, skill, and hook together): `/plugin disable compound-memory@compound-memory`

Optional snippet for your project `CODEBUDDY.md` (skip it when the hook reminder already appears):

```markdown
compound-memory: COMPOUND_MEMORY_AGENT_ID defaults to agent-workbuddy in .mcp.json (overridable); source and agent use that same id. memory_search comes before non-trivial work; memory_feedback after an adopted hit is what compounds the store. Work inside a repo fits project-scoped memories. Five MCP tools; this plugin does not reimplement them.
```

## 安装（简体中文）

需要已安装 WorkBuddy 或 CodeBuddy，且 `uv` / `uvx` 在 `PATH` 上。

```text
/plugin marketplace add chinwe/compound-memory
/plugin install compound-memory@compound-memory
```

插件默认 `COMPOUND_MEMORY_AGENT_ID=agent-workbuddy`，宿主可以覆盖。记忆库初始化：`uvx --from compound-memory compound-memory init`。市场文件是仓库根 `.codebuddy-plugin/marketplace.json`，不进 PyPI。手写 `~/.workbuddy/mcp.json` 仍然可用。

新会话会跑 SessionStart hook（matcher `startup`），只注入上面英文片段里的几条硬规则，不读记忆库、不调用模型。任务开始仍要先 `memory_search`。

提醒没出现时，把上一节片段贴进项目 `CODEBUDDY.md`（没有该文件时宿主会读 `AGENTS.md`）。关掉全部 hook：在 `~/.codebuddy/settings.json` 里设 `"disableAllHooks": true`。只停用本插件：`/plugin disable compound-memory@compound-memory`。
