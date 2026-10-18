# dsh-compound-memory

English | [中文](#中文)

DeepSeek Harness bundle that launches the existing [compound-memory](https://pypi.org/project/compound-memory/) MCP server. It does not reimplement memory in JavaScript and it does not add a sixth MCP tool.

The package is a configuration layer (`dsh.bundle`). Its patch inserts one in-box `@deepseek-ai/dsh-mcp-client` row. DSH starts `uvx --from compound-memory compound-memory-server` and exposes the server's five tools as:

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

From a checkout of this repository:

```bash
dsh plugin --profile web add /absolute/path/to/compound-memory/integrations/dsh
```

After the npm package is published (not done from this repository automatically):

```bash
dsh plugin --profile web add dsh-compound-memory
```

From GitHub, once this directory is on the default branch:

```bash
dsh plugin --profile web add github:chinwe/compound-memory#path:/integrations/dsh
```

Restart `dsh`. `dsh --profile web --dump-config` should show a `# == dsh-compound-memory` layer and the `mcp-compound-memory` row.

## Configuration

Set these in the environment that launches `dsh`. The patch reads them when the config loads. Unset or blank falls back to the default. Nothing here is a secret.

| Variable | Default when unset |
|---|---|
| `COMPOUND_MEMORY_ROOT` | `$HOME/.agents/memory` (the Python server expands a leading `$HOME`) |
| `COMPOUND_MEMORY_AGENT_ID` | `agent-deepseek` |

`agent-deepseek` is the host id already used by `skills/compound-memory/SKILL.md` and `docs/agent-integration.md`. Cross-host validation treats that string as this host. Override it only when this process should be a different host.

`uvx` must be on `PATH`. A later patch layer replaces this row's whole `config` (it does not deep-merge). To point `command` at an absolute `uvx`, restate the row in `~/.dsh/profiles/web/cordis.patch.yml`:

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
      COMPOUND_MEMORY_AGENT_ID: agent-deepseek
    failOnStartupError: true
```

`engines.dsh` is `>=0.2.0-rc.1 <0.3.0-0`. DSH checks that range with prereleases included, so npm `latest` `0.2.0-rc.2` and npm `next` `0.2.1-rc.2` both match, and a `0.3.0` prerelease does not. This package does not depend on `@deepseek-ai/*`: the MCP client is already in the running harness, and the patch only names it. There is no `peerDependencies` entry to satisfy.

## Usage loop

Same five tools as every other host. On a non-trivial task, call `memory_search` first. After a hit is actually used, call `memory_feedback` with `agent` `agent-deepseek` (or whatever `COMPOUND_MEMORY_AGENT_ID` is). Write only stable facts. The full rules are in [`skills/compound-memory/SKILL.md`](../../skills/compound-memory/SKILL.md).

## 中文

这是 DeepSeek Harness 的配置层 bundle，用来拉起 PyPI 上已有的 compound-memory MCP server。不在 JS 里重写记忆，也不增加第 6 个 MCP tool。

`cordis.patch.yml` 插入一行宿主自带的 `@deepseek-ai/dsh-mcp-client`。DSH 执行 `uvx --from compound-memory compound-memory-server`，五个工具在模型侧的名字是 `mcp__compound-memory__memory_search`、`memory_get`、`memory_write`、`memory_link`、`memory_feedback`。`memory_search` 的返回体含 `{"hits": [...]}`。

安装需要 `dsh` CLI、启动 `dsh` 时 `PATH` 上有 `uvx`，以及首次拉取 PyPI 包的网络。空库先初始化：

```bash
uvx --from compound-memory compound-memory init
```

本仓库：

```bash
dsh plugin --profile web add /compound-memory 仓库的绝对路径/integrations/dsh
```

npm 发布之后（本仓库不会自动发布）：

```bash
dsh plugin --profile web add dsh-compound-memory
```

GitHub（该目录已在默认分支上时）：

```bash
dsh plugin --profile web add github:chinwe/compound-memory#path:/integrations/dsh
```

重启 `dsh`。`dsh --profile web --dump-config` 里应出现 `# == dsh-compound-memory` 与 `mcp-compound-memory`。

在启动 `dsh` 的环境里设置变量。patch 在加载配置时读取；未设置或空白则用默认值。这里没有密钥。

| 变量 | 未设置时的默认值 |
|---|---|
| `COMPOUND_MEMORY_ROOT` | `$HOME/.agents/memory`（`$HOME` 由 Python 程序展开） |
| `COMPOUND_MEMORY_AGENT_ID` | `agent-deepseek` |

`agent-deepseek` 是本仓库 skill 与接入文档里 DeepSeek Harness 的宿主 id。跨宿主验证认的是这个字符串。只有当这个进程应当算另一个宿主时才覆盖它。

`uvx` 必须在 `PATH` 上。后写入的 patch 按 id 整行替换 `config`，不是深合并。要改成绝对路径时，在 `~/.dsh/profiles/web/cordis.patch.yml` 里按英文小节的例子重写整行。

`engines.dsh` 为 `>=0.2.0-rc.1 <0.3.0-0`。DSH 按包含预发布版本的规则检查：npm `latest` 的 `0.2.0-rc.2` 与 `next` 的 `0.2.1-rc.2` 都匹配，`0.3.0` 的预发布版本不匹配。本包不 import `@deepseek-ai/*`，MCP client 用的是正在运行的 harness 里那一份，因此没有 `peerDependencies`。

使用闭环与其他宿主相同：先 `memory_search`，采纳后 `memory_feedback`（`agent` 填 `agent-deepseek` 或实际的 `COMPOUND_MEMORY_AGENT_ID`），只写稳定事实。规范在 [`skills/compound-memory/SKILL.md`](../../skills/compound-memory/SKILL.md)。
