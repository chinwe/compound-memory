# compound-memory (ZCode plugin)

Local multi-agent shared memory for ZCode. This plugin is a thin stdio wrapper around the published PyPI package `compound-memory`. It does not reimplement the five MCP tools and does not add an in-process LLM.

Requires the [ZCode](https://z.ai) desktop app and [`uv`](https://docs.astral.sh/uv/) (`uvx` on `PATH`).

## Install

ZCode reads the repository-root `.claude-plugin/marketplace.json` as its marketplace catalog (its loader checks `.claude-plugin/marketplace.json` before a root-level `marketplace.json`). Clone this repository, then in the ZCode desktop app:

1. Open **插件市场 / Plugin Marketplace → 添加 / Add → 添加插件市场 / Add Plugin Marketplace**.
2. Paste the local clone path of this repository (the directory that contains `.claude-plugin/`), then click **添加 / Add**.
3. Open **个人 / Personal → compound-memory**, check the details of **Compound Memory (ZCode)**, and click **安装 / Install**.

The plugin launches `uvx --from compound-memory compound-memory-server` with `COMPOUND_MEMORY_AGENT_ID=agent-zcode`. Override that env var in the host if you need a different id. Initialize the store once:

```bash
uvx --from compound-memory compound-memory init
```

The marketplace entry (`compound-memory-zcode`) lives in `.claude-plugin/marketplace.json` next to the Claude Code entry; the plugin root is this directory (`integrations/zcode`). Those files are not in the PyPI sdist or wheel.

The plugin's MCP server is named `compound-memory`. If `~/.zcode/cli/config.json` already registers a server with that name (the hand-written path, §3 of `docs/agent-integration.md`), remove one of the two before installing to avoid a duplicate. The hand-written path additionally runs a SessionStart recall hook (`session_start.py`) that injects recent memories; this plugin does not replace that — it only adds a short hard-rule reminder.

The short skill shipped with this plugin is [`skills/compound-memory/SKILL.md`](skills/compound-memory/SKILL.md). The canonical long-form skill is [`../../skills/compound-memory/SKILL.md`](../../skills/compound-memory/SKILL.md).

## Session start

On a new session, `hooks/hooks.json` runs `hooks/session-start.sh` (`SessionStart`, matcher `startup`). The script prints a few hard rules as `hookSpecificOutput.additionalContext`. It does not read the memory store and does not call a model. It is a reminder only — unlike the hand-written path's recall hook, it does not bring memories back. Search before non-trivial work is still required.

After installing, start a new session and confirm the reminder is in context. If it is missing (ZCode may not run plugin SessionStart hooks on every build), paste the snippet below into `~/.zcode/AGENTS.md` (user level) or the project `AGENTS.md`.

Turn the hook or plugin off:

- All plugin hooks and servers at once: uninstall or disable the plugin under **设置 → 插件 / Settings → Plugins**.
- This plugin only: **Settings → Plugins → Compound Memory (ZCode) → Disable**.

Optional snippet for your `AGENTS.md` (skip it when the hook reminder already appears):

```markdown
compound-memory: COMPOUND_MEMORY_AGENT_ID defaults to agent-zcode in the plugin manifest (overridable); source and agent use that same id. memory_search comes before non-trivial work; memory_feedback after an adopted hit is what compounds the store. Work inside a repo fits project-scoped memories. Five MCP tools; this plugin does not reimplement them.
```

## 安装（简体中文）

需要已安装 ZCode 桌面端，且 `uv` / `uvx` 在 `PATH` 上。

市场目录就是本仓库：ZCode 的市场加载顺序是先 `.claude-plugin/marketplace.json`、再仓库根级 `marketplace.json`，本仓库用的是前者。克隆本仓库后：

1. 打开 **插件市场 → 添加 → 添加插件市场**，粘贴本仓库的本地克隆路径（含 `.claude-plugin/` 的目录），点添加。
2. 在 **个人 → compound-memory** 里找到 **Compound Memory (ZCode)**，确认版本与描述后点安装。

插件用 `uvx --from compound-memory compound-memory-server` 拉起，默认 `COMPOUND_MEMORY_AGENT_ID=agent-zcode`（宿主可覆盖）。记忆库初始化：`uvx --from compound-memory compound-memory init`。市场条目（`compound-memory-zcode`）与 Claude Code 条目同在 `.claude-plugin/marketplace.json`，不进 PyPI sdist / wheel。

插件的 MCP server 名叫 `compound-memory`。若 `~/.zcode/cli/config.json` 已手写同名 server（见 `docs/agent-integration.md` §3），先二选一，避免重复注册。手写路径另有 session_start.py 召回 hook；本插件不替代它，只追加一条硬规则提醒。

新会话会跑 SessionStart hook（matcher `startup`），只注入几条硬规则，不读记忆库、不调用模型——不带召回，任务开始仍要先 `memory_search`。装完开一次新会话确认提醒出现；没出现就把上面英文片段贴进 `~/.zcode/AGENTS.md` 或项目 `AGENTS.md`。

停用：**设置 → 插件**里禁用或卸载本插件。
