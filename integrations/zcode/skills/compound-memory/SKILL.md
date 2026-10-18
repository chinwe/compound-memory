---
name: compound-memory
description: Use the local compound-memory MCP tools for shared cross-agent memory. Search before non-trivial work (memory_search), fetch a hit by id (memory_get), record a stable fact (memory_write), link related memories (memory_link), and report what happened after you adopt a hit (memory_feedback).
---

# compound-memory

Thin ZCode wrapper around the published PyPI server (`uvx --from compound-memory compound-memory-server`). Do not reimplement the tools. Exactly five MCP tools:

| Tool | When |
|---|---|
| `memory_search` | Before non-trivial work. Returns `{"hits", "count", "retrieval"}`. Default `top_k` is 5. |
| `memory_get` | Fetch one memory by id. The result always has `found`. |
| `memory_write` | Store a stable fact, convention, or pitfall. `source` is this host's agent id. `fact` / `insight` / `decision` need a stable `key`. |
| `memory_link` | Bidirectional link. Both memories must share a namespace. |
| `memory_feedback` | After you actually use a hit. This is what makes the store compound. |

`COMPOUND_MEMORY_AGENT_ID` defaults to `agent-zcode` (this plugin's manifest). The host may override it. Pass that same id as `source` / `agent`.

A SessionStart hook may inject a few hard rules on a new session. This skill is the on-demand source for when and how.

`uv` and `uvx` must be on `PATH`.

Deep rules (types, keys, namespaces, project scope, feedback outcomes): [docs/agent-integration.en.md](https://github.com/chinwe/compound-memory/blob/main/docs/agent-integration.en.md) and the canonical skill [skills/compound-memory/SKILL.md](https://github.com/chinwe/compound-memory/blob/main/skills/compound-memory/SKILL.md). When they disagree, the canonical skill wins.
