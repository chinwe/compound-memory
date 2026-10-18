#!/bin/sh
# SessionStart：只打印几条硬规则（additionalContext）。不读记忆库，不调用模型。
set -eu
printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":"compound-memory: COMPOUND_MEMORY_AGENT_ID defaults to agent-zcode in the plugin manifest (overridable); source and agent use that same id. memory_search comes before non-trivial work; memory_feedback after an adopted hit is what compounds the store. Work inside a repo fits project-scoped memories. Five MCP tools; this plugin does not reimplement them."}}'
