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

The short skill shipped with this plugin is [`skills/compound-memory/SKILL.md`](skills/compound-memory/SKILL.md). The canonical long-form skill is [`../../skills/compound-memory/SKILL.md`](../../skills/compound-memory/SKILL.md).

## 安装（简体中文）

需要已安装 Claude Code，且 `uv` / `uvx` 在 `PATH` 上。

```bash
claude plugin marketplace add chinwe/compound-memory
claude plugin install compound-memory@compound-memory
```

插件默认 `COMPOUND_MEMORY_AGENT_ID=agent-claude`，宿主可以覆盖。记忆库初始化：`uvx --from compound-memory compound-memory init`。不提交 Anthropic 官方 Directory。
