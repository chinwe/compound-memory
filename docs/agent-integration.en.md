# Agent integration (English)

compound-memory is a local multi-agent memory store at `~/.agents/memory`. Every host uses the same MCP server — exactly five tools — and the same CLI.

This page is the short setup: install, the required agent id, the five tools, and the search → feedback → write loop. Per-host config files, namespaces, project scope, distillation, and troubleshooting stay in the full Chinese guide, [`agent-integration.md`](agent-integration.md). The rules injected into a host live in [`skills/compound-memory/SKILL.md`](../skills/compound-memory/SKILL.md). When this page and the skill disagree, the skill wins.

## Install with uvx

Python ≥ 3.11. A normal install does not clone the repo.

```bash
uvx --from compound-memory compound-memory init
```

That creates `~/.agents/memory`. Set `COMPOUND_MEMORY_ROOT` to override it. A leading `~` or `$HOME` is expanded; a blank value falls back to the default.

Register a stdio MCP server. `uvx` takes the PyPI package name; the console script is `compound-memory-server`, so `--from` is required. uv writes its own logs to stderr and does not break the MCP stdio stream. If the host cannot find `uvx` on `PATH`, set `command` to the absolute path `~/.local/bin/uvx` (hosts do not expand `~` in `command`).

```json
{
  "mcpServers": {
    "compound-memory": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "compound-memory", "compound-memory-server"],
      "env": {
        "COMPOUND_MEMORY_ROOT": "$HOME/.agents/memory",
        "COMPOUND_MEMORY_AGENT_ID": "agent-<your-host-id>"
      }
    }
  }
}
```

`COMPOUND_MEMORY_AGENT_ID` is **required** whenever more than one agent shares the store. The process identity wins over whatever the model claims as `source`, `reader`, or `agent`, so a forged id cannot write another host's namespace or inflate cross-host validation. Unset or blank is a hard failure for shared `_shared` use: CLI `stats` and `doctor` exit 1 (the diagnosis is still printed). MCP tools keep running, and the server logs the same warning at startup. Without the env var, identity is self-reported and can be forged.

Use a stable host id, and pass that same string as `source` / `agent` / `reader`:

| Host | `COMPOUND_MEMORY_AGENT_ID` |
|---|---|
| WorkBuddy | `agent-workbuddy` |
| CodeBuddy | `agent-workbuddy` |
| ZCode | `agent-zcode` |
| Claude Code | `agent-claude` |
| Cursor | `agent-cursor` |
| DeepSeek Harness | `agent-deepseek` |

WorkBuddy and CodeBuddy can install the plugin from this repository instead of pasting MCP JSON: `/plugin marketplace add chinwe/compound-memory`, then `/plugin install compound-memory@compound-memory`. That sets `COMPOUND_MEMORY_AGENT_ID=agent-workbuddy`. Install notes: [`integrations/workbuddy/README.md`](../integrations/workbuddy/README.md).

ZCode can install a plugin from this repository instead of pasting MCP JSON: clone it, then in the desktop app add the clone path as a plugin marketplace (**Plugin Marketplace → Add → Add Plugin Marketplace**) and install **Compound Memory (ZCode)** under **Personal → compound-memory**. ZCode reads the repository-root `.claude-plugin/marketplace.json` as its catalog. That sets `COMPOUND_MEMORY_AGENT_ID=agent-zcode`. The plugin's MCP server is named `compound-memory`; keep only one of it and the hand-written `~/.zcode/cli/config.json` server. Install notes: [`integrations/zcode/README.md`](../integrations/zcode/README.md).

**Verify:** ask the agent to call `memory_search` (any keyword). A `{"hits": [...]}` body means the server is connected. Or run `uvx --from compound-memory compound-memory doctor` (JSON on stdout, warnings on stderr). Restart the host if it loads MCP servers only at startup.

`doctor` exits 0 when no hard problem is present, and exits 1 when `COMPOUND_MEMORY_AGENT_ID` is unset or blank, the store root is missing or not a directory, the write lock cannot be acquired, or the memory root is not a usable git repository (git missing, no `.git`, or `git status` failed). A lexical retrieval mode and a dirty worktree are reported and do not by themselves fail the check. A `write_lock` hard failure is that probe, including when the lock file cannot be created; on the write path, a lock file that cannot be created still warns and may write unlocked.

A local checkout is the developer path. Clone, `uv sync --extra dev`, then point the host at `uv` with args `["run", "--directory", "<absolute-repo-path>", "compound-memory-server"]`. Details are in the Chinese guide, §1.

## Five MCP tools

These five tools are the only read-write boundary. Do not add a sixth.

| Tool | What it does |
|---|---|
| `memory_search` | Ranked hits: `{"hits", "count", "retrieval"}`. Default `top_k` is 5. Default scope is `_shared` plus your own private namespace. Omit `project` and only global memories are returned. |
| `memory_get` | Fetch one memory by id. The result always has `found`. Private namespaces require `reader`. |
| `memory_write` | Store a memory. `type` is `episode`, `fact`, `insight`, `skill`, or `decision`. `source` is your agent id. Give fact / insight / decision a stable `key`. `content` over 262144 characters is rejected and nothing is written. |
| `memory_link` | Bidirectional link. Both memories must be in the same namespace. Private namespaces accept links only from the owner. |
| `memory_feedback` | Report what happened after you actually used a hit. This is what makes the store compound. |

Tool descriptions already carry the loop, so an agent that only reads the tool list still gets the rules. Injecting the skill tightens what gets written.

## Loop: search → feedback → write

1. **Search first.** On a non-trivial task, call `memory_search` with the task keywords (preferences, project conventions, environment pitfalls) before acting.
2. **Feedback after you use a hit.** Call `memory_feedback` with your agent id. Default `outcome` is `success` (confidence +0.1; the first success from a different host adds +0.15 once). Use `failure` when the memory misled you (−0.2, floor 0.05), `contradiction` when you dispute it (confidence frozen, review queue), `obsolete` when it has been superseded (archived immediately), or `unknown` when you only want the event recorded. Every outcome advances `uses` and `last_used`. Skipping feedback leaves the store static.
3. **Write only stable facts** at the end of the session: preferences, conventions, environment constraints, pitfalls. Do not write scratch notes, in-progress status, or one-off session details. Reuse an existing `key` when you are updating a fact you already stored.

When a new memory is caused by or derived from an existing one, `memory_link` them (same namespace). Neighbors that come back on search or get are clues; adopt the hit itself.

Type choice, `project` slugs, private `agent-*` namespaces, and lesson / avoid marks are in the Chinese guide §6 and in the skill.

## Next

- How to record whether the install runs, and whether the same task changes once memory is populated: [`eval/README.md`](eval/README.md). Checklist only — not a measured effectiveness claim.
- Full per-host configs (WorkBuddy, ZCode, Claude Code, Cursor, DeepSeek Harness), CLI operations, and troubleshooting: [`agent-integration.md`](agent-integration.md)
- WorkBuddy / CodeBuddy plugin: [`integrations/workbuddy/README.md`](../integrations/workbuddy/README.md)
- Usage rules to inject: [`skills/compound-memory/SKILL.md`](../skills/compound-memory/SKILL.md)
- Product overview: [`README.md`](../README.md)
