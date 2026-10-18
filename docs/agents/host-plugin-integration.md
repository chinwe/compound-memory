# Host plugin integration

Marketplace plugins let a host install compound-memory instead of hand-pasting MCP JSON. Each host gets one thin plugin under `integrations/<host>/`; the plugin only launches the published PyPI server (`uvx --from compound-memory compound-memory-server`) with that host's `COMPOUND_MEMORY_AGENT_ID`, plus a SessionStart hard-rule hook and a short skill. It never reimplements the five MCP tools.

## Which marketplace file each host reads

The deciding fact: **where the host's loader looks for a marketplace catalog**. Do not invent a new catalog file before checking this — a catalog the loader never reads is dead weight, and two catalogs for one loader silently shadow each other.

| Host | Catalog the loader reads | Plugin root |
|---|---|---|
| Claude Code | `.claude-plugin/marketplace.json` | `integrations/claude` |
| ZCode | `.claude-plugin/marketplace.json` first, then a root-level `marketplace.json` | `integrations/zcode` |
| WorkBuddy / CodeBuddy | `.codebuddy-plugin/marketplace.json` | `integrations/workbuddy` |

ZCode and Claude Code therefore share one catalog file; ZCode's plugin is a second entry there (`compound-memory-zcode`). A `.zcode-plugin/marketplace.json` is **not** recognized. The load order was verified against the ZCode app bundle (`zcode.cjs`, the `$dt` helper: explicit path → `.claude-plugin/marketplace.json` → root-level `marketplace.json`); re-verify against the installed build when touching this decision (`grep -o 'marketplace.json' /Applications/ZCode.app/Contents/Resources/glm/zcode.cjs`).

ZCode plugin manifests differ from Claude's: the MCP server config is **inline** in `.zcode-plugin/plugin.json` (`mcpServers` key — every official ZCode plugin does this; there are no `.mcp.json`-pointer examples), and hook commands use the `${CLAUDE_PLUGIN_ROOT}` placeholder. The reference doc bundled with the local `plugin-creator` skill is the format authority: `~/.zcode/cli/plugins/cache/zcode-plugins-official/plugin-creator/*/skills/plugin-creator/references/plugin-json-spec.md`.

## Checklist when adding or changing a host plugin

1. Plugin tree: `.zcode-plugin|claude-plugin|codebuddy-plugin/plugin.json`, `hooks/hooks.json` + `hooks/session-start.sh` (executable, `#!/bin/sh`, prints `hookSpecificOutput.additionalContext` only), `skills/compound-memory/SKILL.md` (copy the sibling host's and change the host id), `README.md`.
2. Catalog entry: `name` and `version` must equal the plugin manifest's — the version drift bit once already (#92).
3. Docs sync (five places): the plugin's own `README.md`, root `README.md` + `README.zh-CN.md` plugin sections, `docs/agent-integration.md` (host table + host section) + `docs/agent-integration.en.md`, `CHANGELOG.md`.
4. No changes needed in `MANIFEST.in` or `scripts/wheel_smoke.sh`: `integrations` is already grafted-then-pruned, and the smoke check's banned member names already cover any `integrations/<host>` tree and `.mcp.json`.
5. If the host already has a hand-written MCP config (e.g. `~/.zcode/cli/config.json`), the plugin's MCP server name (`compound-memory`) collides with it — document that the user keeps one of the two, and note what the hand-written path has that the plugin does not (ZCode: the `session_start.py` recall hook).

## Checks

- `uv run python scripts/validate_plugins.py` — mechanical validation run in CI (ubuntu × 3.13): catalog ↔ manifest name/version agreement, source directories, component paths inside the plugin root, hook command placeholder + referenced script existence + executable bit, no repo-root `.mcp.json`. Add a case there when you add a new structural rule.
- Host-side authority: `claude plugin validate --strict` for the Claude catalog; ZCode has no CLI in a desktop install — installation is a UI action (Plugin Marketplace → Add → Add Plugin Marketplace → paste the clone path).
