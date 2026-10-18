# dsh-compound-memory

English | [中文](#中文)

DeepSeek Harness bundle that launches the existing [compound-memory](https://pypi.org/project/compound-memory/) MCP server. It does not reimplement memory in JavaScript, it does not add a sixth MCP tool, and it has no install or lifecycle scripts.

This directory is the installable unit. Its own `package.json` declares `dsh.bundle`. While it is staged inside [chinwe/compound-memory](https://github.com/chinwe/compound-memory) at `integrations/dsh/`, that parent repository is not the plugin: `dsh plugin --profile web add github:chinwe/compound-memory` installs the Python repo root, which has no `dsh.bundle`. Copy this directory verbatim to the root of `chinwe/dsh-compound-memory` when that public repo exists (it is not created from here). `cordis.patch.yml` is the layer `dsh.bundle` points at. `skills/compound-memory/SKILL.md` is the usage spec shipped with the package.

The awesome-dsh-plugin gate builds the install command from the entry URL. An entry that points at a repository root is rejected when that root `package.json` has no `dsh.bundle`, even if a subdirectory has one, because `dsh plugin --profile web add github:owner/repo` would install nothing ([check-submission.mjs](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/blob/main/scripts/check-submission.mjs), `hasBundle`). The same check tells authors they may instead point the entry at the subdirectory ([contributing.md](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/blob/main/contributing.md) still documents monorepo subpackages). This bundle is staged as its own future root so the catalog URL, the `dsh-plugin` topic (a repository topic, requested in that contributing guide), and `dsh plugin add github:chinwe/dsh-compound-memory` all name the same root.

## What a bundle is

A bundle is an npm package whose `package.json` answers "what does this package contribute?" with `dsh.bundle`. A profile (`dsh.profile`) is a separate manifest: it lists which bundles to compose. Official shape, from [Package and install a plugin](https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/publish):

```json
"dsh": { "bundle": { "patch": "./cordis.patch.yml" } }
```

`patch` is a path, or an ordered list of paths, to a YAML array of patch operations. This package uses one file and one operation: `insert` one row. Later layers replace a row's whole `config` by `id`; they do not deep-merge keys (same page, "The loading order").

The inserted row names the in-box client `@deepseek-ai/dsh-mcp-client`. Its stdio `config` is `serverName`, `transport`, `command`, `args`, `env`, and `failOnStartupError`, documented in the [mcp-client README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md). This package does not import that client and does not declare `@deepseek-ai/*` peer dependencies. In-box names resolve from the running DSH install.

DSH starts:

```bash
uvx --from compound-memory compound-memory-server
```

The five tools appear as:

| MCP tool | Name inside DSH |
|---|---|
| `memory_search` | `mcp__compound-memory__memory_search` |
| `memory_get` | `mcp__compound-memory__memory_get` |
| `memory_write` | `mcp__compound-memory__memory_write` |
| `memory_link` | `mcp__compound-memory__memory_link` |
| `memory_feedback` | `mcp__compound-memory__memory_feedback` |

`memory_search` returns `{"hits": [...], "count", "retrieval"}`.

## Install

Requires the `dsh` CLI ([install](https://deepseek-harness.github.io/deepseek-harness/en/guide/)), `uv`/`uvx` on the `PATH` that starts `dsh`, and network access the first time `uvx` fetches `compound-memory` from PyPI. Initialize the store once if it does not exist yet:

```bash
uvx --from compound-memory compound-memory init
```

From a checkout of compound-memory, add this directory by path (the same command works after the directory has been copied to its own repo root):

```bash
dsh plugin --profile web add /absolute/path/to/integrations/dsh
```

After the owner creates the public repo and pushes this directory as its root (no `#path`, and no `prepare` / `allowBuilds` step, because this package has no scripts):

```bash
dsh plugin --profile web add github:chinwe/dsh-compound-memory
```

After an npm publish from that repo root (not done from here):

```bash
dsh plugin --profile web add dsh-compound-memory
```

Restart `dsh`. `dsh --profile web --dump-config` should show a `# == dsh-compound-memory` layer and the `mcp-compound-memory` row.

## Configuration

Set these in the environment that launches `dsh`. The patch reads them with `!!js` when the config loads. Unset or blank falls back to the default. Nothing here is a secret. The stdio child starts from a scrubbed parent environment; configured `env` is merged on top ([mcp-client README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)).

| Variable | Default when unset |
|---|---|
| `COMPOUND_MEMORY_ROOT` | `$HOME/.agents/memory` (the Python server expands a leading `$HOME`) |
| `COMPOUND_MEMORY_AGENT_ID` | `agent-dsh` |

`agent-dsh` is this host's stable id. Cross-host validation treats that string as DeepSeek Harness. Override it in the launching environment when this process should be a different host. A blank value is treated as unset, so the default still applies.

`uvx` must be on `PATH`. A later patch layer replaces this row's whole `config`. To point `command` at an absolute `uvx`, restate the row in `~/.dsh/profiles/web/cordis.patch.yml`:

```yaml
- id: mcp-compound-memory
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: compound-memory
    transport: stdio
    command: /absolute/path/to/uvx
    args: ['--from', 'compound-memory', 'compound-memory-server']
    env:
      COMPOUND_MEMORY_ROOT: "$HOME/.agents/memory"
      COMPOUND_MEMORY_AGENT_ID: agent-dsh
    failOnStartupError: true
```

`engines.dsh` is `>=0.2.0-rc.1 <0.3.0-0`. DSH checks that range with prereleases included ([discussion #9194](https://github.com/deepseek-ai/deepseek-harness/discussions/9194)), so npm `latest` `0.2.0-rc.2` and npm `next` `0.2.1-rc.2` both match, and a `0.3.0` prerelease does not. The publish guide's example `>=0.2.0 <0.3.0` excludes `0.2.0-rc.2` because that prerelease sorts before `0.2.0`; the lower bound here includes the `0.2.0-rc.1` line on purpose.

## Skill

The usage spec is [`skills/compound-memory/SKILL.md`](skills/compound-memory/SKILL.md). On a non-trivial task, call `memory_search` first. After a hit is actually used, call `memory_feedback` with `agent` `agent-dsh` (or whatever `COMPOUND_MEMORY_AGENT_ID` is). Write only stable facts.

DSH does not load that file just because it is inside the npm package. The filesystem skill provider scans only the direct children of each skill root: `<root>/<name>/SKILL.md` or a flat `<name>.md`. Nested `**/SKILL.md` is not discovered ([skills subsystem](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)). Default roots are `<project>/.dsh/skills`, `<project>/.agents/skills`, `$DSH_HOME/skills` (`~/.dsh/skills`), and `$DSH_AGENTS_HOME/skills` (`~/.agents/skills`). This package ships no `scripts`, so install does not copy the skill. Copy or symlink `skills/compound-memory` to one of those roots, for example `~/.dsh/skills/compound-memory`.

## 中文

这是 DeepSeek Harness 的配置层 bundle，用来拉起 PyPI 上已有的 compound-memory MCP server。不在 JS 里重写记忆，不增加第 6 个 MCP tool，也没有 install / lifecycle 脚本。

本目录才是可安装单元，它自己的 `package.json` 声明了 `dsh.bundle`。它目前暂存在 [chinwe/compound-memory](https://github.com/chinwe/compound-memory) 的 `integrations/dsh/` 里，那个父仓库不是插件：`dsh plugin --profile web add github:chinwe/compound-memory` 装的是 Python 仓库根，根上没有 `dsh.bundle`。公开仓库 `chinwe/dsh-compound-memory` 建好之后，把本目录原样复制成它的根（不在这里创建该仓库）。`cordis.patch.yml` 是 `dsh.bundle` 指向的层，`skills/compound-memory/SKILL.md` 是随包使用规范。

awesome-dsh-plugin 的门禁按条目 URL 生成安装命令。条目指向仓库根、而根上的 `package.json` 没有 `dsh.bundle` 时，即使子目录里有，也会被拒绝，因为 `dsh plugin --profile web add github:owner/repo` 什么都装不上（[check-submission.mjs](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/blob/main/scripts/check-submission.mjs) 的 `hasBundle`）。同一段检查会建议改指子目录（[contributing.md](https://github.com/awesome-dsh-plugin/awesome-dsh-plugin/blob/main/contributing.md) 仍允许 monorepo 子包）。本 bundle 按独立仓库的根来暂存，是为了让目录 URL、`dsh-plugin` topic（仓库级 topic，贡献指南要求加上）和 `dsh plugin add github:chinwe/dsh-compound-memory` 指的是同一个根。

官方形状见 [Package and install a plugin](https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/publish)：`dsh.bundle.patch` 指向一份 YAML 数组（或按顺序应用的多份文件）。本包只有一次 `insert`，插入宿主自带的 `@deepseek-ai/dsh-mcp-client`。stdio 的 `config` 字段见 [mcp-client README](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/mcp/mcp-client/README.md)。后一层按 id 整行替换 `config`，不是深合并。本包不 import `@deepseek-ai/*`，也没有 `peerDependencies`；盒子里的包名从正在运行的 DSH 安装解析。

DSH 执行 `uvx --from compound-memory compound-memory-server`。五个工具在模型侧叫 `mcp__compound-memory__memory_search`、`memory_get`、`memory_write`、`memory_link`、`memory_feedback`。`memory_search` 的返回体含 `{"hits": [...]}`。

安装需要 `dsh` CLI、启动 `dsh` 时 `PATH` 上有 `uvx`，以及首次拉取 PyPI 包的网络。空库先初始化：

```bash
uvx --from compound-memory compound-memory init
```

compound-memory 检出（复制成独立仓库根之后，命令相同，只是路径变了）：

```bash
dsh plugin --profile web add /integrations/dsh 的绝对路径
```

所有者创建公开仓库并把本目录作为根推上去之后（没有 `#path`；没有 `prepare`，也不需要 `allowBuilds`，因为没有 scripts）：

```bash
dsh plugin --profile web add github:chinwe/dsh-compound-memory
```

从该仓库根 `npm publish` 之后（这里不发布）：

```bash
dsh plugin --profile web add dsh-compound-memory
```

重启 `dsh`。`dsh --profile web --dump-config` 里应出现 `# == dsh-compound-memory` 与 `mcp-compound-memory`。

在启动 `dsh` 的环境里设置变量。patch 用 `!!js` 在加载配置时读取；未设置或空白则用默认值。这里没有密钥。stdio 子进程从清洗过的父环境启动，配置里的 `env` 再叠上去。

| 变量 | 未设置时的默认值 |
|---|---|
| `COMPOUND_MEMORY_ROOT` | `$HOME/.agents/memory`（`$HOME` 由 Python 程序展开） |
| `COMPOUND_MEMORY_AGENT_ID` | `agent-dsh` |

`agent-dsh` 是这个宿主的稳定 id。跨宿主验证把这个字符串认作 DeepSeek Harness。只有当这个进程应当算另一个宿主时，才在启动环境里覆盖它。空白值按未设置处理，默认值仍然生效。

`uvx` 必须在 `PATH` 上。要改成绝对路径时，在 `~/.dsh/profiles/web/cordis.patch.yml` 里按英文小节的例子重写整行。

`engines.dsh` 为 `>=0.2.0-rc.1 <0.3.0-0`。DSH 按包含预发布版本的规则检查（[discussion #9194](https://github.com/deepseek-ai/deepseek-harness/discussions/9194)）：npm `latest` 的 `0.2.0-rc.2` 与 `next` 的 `0.2.1-rc.2` 都匹配，`0.3.0` 的预发布版本不匹配。发布文档里的例子 `>=0.2.0 <0.3.0` 会排除 `0.2.0-rc.2`，因为该预发布版本排在 `0.2.0` 之前；这里的下界故意包含 `0.2.0-rc.1` 这一线。

使用规范在 [`skills/compound-memory/SKILL.md`](skills/compound-memory/SKILL.md)：先 `memory_search`，采纳后 `memory_feedback`（`agent` 填 `agent-dsh` 或实际的 `COMPOUND_MEMORY_AGENT_ID`），只写稳定事实。

DSH 不会因为文件在 npm 包里就加载它。文件系统 skill 提供方只扫描每个根的直接子项：`<根>/<name>/SKILL.md` 或平铺的 `<name>.md`，不发现嵌套的 `**/SKILL.md`（[skills 子系统](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/skills.md)）。默认根是项目 `.dsh/skills`、项目 `.agents/skills`、`$DSH_HOME/skills`（`~/.dsh/skills`）和 `$DSH_AGENTS_HOME/skills`（`~/.agents/skills`）。本包没有 `scripts`，安装不会复制 skill。把 `skills/compound-memory` 复制或软链到其中一个根，例如 `~/.dsh/skills/compound-memory`。
