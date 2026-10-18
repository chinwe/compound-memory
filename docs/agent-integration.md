# Agent 接入与使用指南

**English:** short setup and the search → feedback → write loop — [`agent-integration.en.md`](agent-integration.en.md). This file is the full per-host guide.

compound-memory 是本地多 Agent 共享记忆库（存储 `~/.agents/memory`）。所有 Agent 宿主通过**同一条 MCP 边界**（恰好 5 个 tool）接入，共用**同一套 CLI** 做运维与蒸馏；本文给出常见宿主的接入配置与统一的使用规范。

> 术语遵循根目录 `CONTEXT.md`；架构与复利机制见 `README.md`。

## 接入一览

| 宿主 | 状态 | MCP 配置位置 | 使用规则注入方式 |
|---|---|---|---|
| WorkBuddy | ✅ 已接入 | `~/.workbuddy/mcp.json` | `~/.workbuddy/MEMORY.md`（用户级记忆，每会话自动注入） |
| ZCode | ✅ 已接入 | `~/.zcode/cli/config.json` | SessionStart hook 自动注入 + `~/.zcode/AGENTS.md` |
| Claude Code | ✅ 已接入 | `~/.claude.json`（用户级） | `~/.claude/CLAUDE.md` + 分发 skill |
| Cursor | ✅ 可接入 | 项目 `.cursor/mcp.json` 或用户 `~/.cursor/mcp.json` | 项目规则指向 `skills/compound-memory/SKILL.md` |
| DeepSeek Harness (dsh) | ✅ 可接入 | `integrations/dsh` bundle，或手写 `~/.dsh/profiles/web/cordis.patch.yml` | agent 指令文件 |

---

## 1. 通用接入参数

所有宿主都是 stdio 方式启动同一个 MCP server，只有三个要素。日常接入用 `uvx` 从 PyPI 拉起，不绑定仓库路径。下文示例中 `~` 表示用户主目录。`command` 写 `uvx`；宿主若找不到它，改成绝对路径 `~/.local/bin/uvx`（宿主原样启动，不展开 `~`）。`COMPOUND_MEMORY_ROOT` 由程序展开前缀 `~` 与 `$HOME` / `${HOME}`，未设置或空白则回退 `$HOME/.agents/memory`；JSON / YAML 的 env 块不经 shell，示例写成 `$HOME/.agents/memory`。

| 项 | 值 |
|---|---|
| 启动命令 | `uvx --from compound-memory compound-memory-server` |
| `COMPOUND_MEMORY_ROOT` | 可省略。`~` 与 `$HOME` 前缀会展开；省略或空白即 `$HOME/.agents/memory` |
| `COMPOUND_MEMORY_AGENT_ID` | **必配**。稳定的本宿主 id（如 `agent-claude`），不可由模型或不可信调用方改写。未设置或空白时自报身份可被伪造，不适合共享 `_shared` 的多 Agent 部署：CLI `stats` 与 `doctor` **退出码 1**（诊断仍打印）。MCP 工具调用不因此中断；server 启动日志与 `stats.attestation.warning` 仍会标明 |

**运行环境（uvx）**：`uvx` 的参数是 PyPI 包名 `compound-memory`，MCP 入口的 console script 是 `compound-memory-server`，两者不同，所以必须 `--from`。不需要克隆仓库，也不要把仓库路径写进宿主配置。uv 自身输出走 stderr，不污染 MCP 的 stdio 协议。

**首次使用**先初始化空库（已有库可跳过）：

```bash
uvx --from compound-memory compound-memory init
```

**验证**：在宿主里让 Agent 调用 `memory_search`（如查询 "compound-memory"），能返回 `{"hits": [...]}` 即接入成功。安装自检跑 `uvx --from compound-memory compound-memory doctor`：`COMPOUND_MEMORY_AGENT_ID` 已设置且记忆库可写、git 可用时退出码 0；未设置或空白时退出码 1。

### 开发 / 从仓库联调

只在克隆本仓库改源码、或要跑尚未发布的本地改动时使用。这不是日常宿主的默认启动方式。

```bash
git clone https://github.com/chinwe/compound-memory.git
cd compound-memory && uv sync --extra dev
uv run --directory <仓库绝对路径> compound-memory init
```

宿主配置改为 `command` = `uv`（不在 PATH 时写 `~/.local/bin/uv`），`args` = `["run", "--directory", "<仓库绝对路径>", "compound-memory-server"]`。`<仓库绝对路径>` 由宿主原样传递，不展开占位符。仓库移动后，每个宿主配置里的 `--directory` 都要改。

### 5 个 MCP tool（唯一读写边界）

| Tool | 用途 | 关键点 |
|---|---|---|
| `memory_write` | 写入记忆 | `type`: episode/fact/insight/skill/decision；`source`: 写入方 agent id；fact/insight/decision 建议带稳定 `key`；可选 `valid_from`/`valid_until`（ISO 日期）标注事实有效期——`valid_until` 已过的事实退出检索结果，但 `memory_get` 仍可读；可选 `project`（小写 slug）标注项目作用域——标注后仅同项目会话检索可见，缺省全局；可选 `lesson_role`（`lesson` 或 `avoid`，省略即普通记忆）。`content` 超过 262144 字符拒绝且不落盘 |
| `memory_search` | 检索 | 返回 `{"hits": [...], "count", "retrieval"}` 按分数排序，缺省 `top_k=5`（要看覆盖面须显式放大）。`retrieval.mode` 为 `vector` 或 `lexical`（向量 extra/模型/引擎不可用或当次召回失败时为 lexical，并带 `reason`），词面降级因此可见。命中自动内嵌最多 3 条一度邻居（正文截到 80 字，不含归档邻居）；`include_neighbors=False` 可关。不传 `ns` 时双通道检索：`_shared` + 调用方自有私有 ns（身份已知时，私有条目自动带出）；显式传 `ns` 只搜该 ns，查 `agent-*` 时必带 `reader`（自己的 agent id），缺省即拒绝。可选 `project`（小写 slug）：**fail-closed**——不传只见全局记忆，传了见 全局 ∪ 该项目。可选 `explain: true`（排障面，#44）：每 hit 附加排序分量对象（词面/向量 rank、RRF 分、先验折算项、检索通道）与证据摘要行，缺省 hit 形状不变。CLI `search` 仍是裸数组，模式看 `stats.retrieval` |
| `memory_get` | 按 id 取回 | 恒含 `found` 键；默认带一度邻居，不设条数上限、正文完整，并保留归档邻居（蒸馏溯源；与 search 的「最多 3 条、截断、排除归档」不同）。`valid_until` 已过的邻居两边都不展开正文。目标在私有 ns 时必带 `reader`，缺省即拒绝。按 id 恒可读（project 不限制 get 本体）；可选 `project` 只用于邻居带出的适用性过滤（邻居=全局 ∪ 该项目） |
| `memory_link` | 双向关联两条记忆 | 复利来源②：关联带出；两条记忆必须同 ns，跨 ns 链被拒绝；私有 ns 记忆仅属主可连（`agent` 填自己的 agent id） |
| `memory_feedback` | 上报"这条记忆被实际采纳了"（可带 outcome） | 每个 outcome 都推进 uses 与 last_used。缺省 `outcome=success`：conf+0.1；**跨宿主首验额外 +0.15**（每宿主每记忆一次）。可选 outcome：`failure` 误导（conf −0.2，地板 0.05，重复累计）；`contradiction` 争议（conf 冻结、入冲突队列待裁决）；`obsolete` 被取代（立即归档）；`unknown` 只记事件，置信度不动。归档记忆被 feedback 自动复活（obsolete 除外）；私有 ns 记忆仅属主可反馈。**采纳后必须调用**；用错了要如实报 failure——置信度是证据正确性，只升不降会掩护错误记忆 |

### 统一约定（各宿主必须一致）

- **`source` agent id**：WorkBuddy → `agent-workbuddy`；ZCode → `agent-zcode`；Claude Code → `agent-claude`；Cursor → `agent-cursor`；DeepSeek Harness → `agent-dsh`。id 用宿主标识而非个性化名字（如 TARS），保证稳定不随命名变化；跨 Agent 验证加分依赖 id 互不相同。这个值必须同时写进 `COMPOUND_MEMORY_AGENT_ID`。
- **namespace**：默认写 `_shared`（全体可见）；`agent-<name>` 是私有区，仅属主可写、读/反馈/关联也须属主身份（`reader`/`agent` 填自己的 agent id，缺省即拒绝）。检索不传 `ns` 时自动并搜自有私有区（双通道）。ns 只允许字符 `[A-Za-z0-9_-]`（ns 会被直接拼进存储路径，含 `../`、`/`、`*` 等一律 ValueError，2026-10-05 审计加固）。日常任务一律用默认值即可。
- **进程身份注入（必配）**：宿主配置的 `env` 必须设 `COMPOUND_MEMORY_AGENT_ID: <本宿主 agent id>`，值稳定且不能由不可信方随意改。注入后存储层以进程身份裁决一切自报身份（source/reader/agent）：缺省自动补真值、等价形式（`agent-x`/`x`）归一化、矛盾响亮拒绝——模型谎报身份失效，伪造 source 污染跨 Agent 验证的通道一并关闭。未注入则退回自报身份：协作边界，不是安全边界，调用方可以冒充其他 agent 的命名空间，**不要**在这种配置下共享 `_shared`。这是共享部署的硬失败：`stats` 与 `doctor` 在未设置或空白时退出码 1（JSON 诊断仍打印到 stdout，告警在 stderr）。MCP 五个工具不因此中断，server 启动日志与 `stats.attestation` 仍把未注入标成 warning。与 ns（`_shared` / `agent-*`）正交的是 `project`（适用性），身份管的是谁能写谁的 ns。
- **project 作用域（按需声明）**：项目专属的记忆（该项目才用的约定/配置/踩坑）写入与检索都带 `project=<slug>`（小写短横线 slug，如 `agenthub`）；跨项目通用的事实不标注。检索缺省 **fail-closed**：不传 `project` 只见全局记忆——「没声明项目 = 全局会话」，项目记忆不外溢进一般会话（ADR 0010）。CLI 侧另有 `COMPOUND_MEMORY_PROJECT` env 回退便利通道（adapter 层读，store 自身不读环境变量；MCP server 全局一份、按项目注入无处落地，显式传参是主语义）。`link` 不限 project（同 ns 即可互链）；蒸馏产物继承源的 project；蒸馏/衰减/复活/stats/review 队列保持库级不过滤。
- **写什么**：稳定事实（用户偏好、项目约定、环境限制、踩坑结论）才写；一次性、会话内临时信息不写。任务状态类（进行时/待办）内容易腐：要么带 `valid_until`、要么改写成不含进行时态的稳定事实——过时的状态记忆比没有更糟。内容用中文，key 用稳定英文短横线标识（如 `user-tts`、`proj-xxx`）。稳态教训才标 `lesson_role`（见下方「教训」表）；借用教训语义，不搬因果引擎。

> 这三条约定与「采纳后必须 feedback」铁律已内嵌在 5 个 MCP tool 的 description 里（server.py），宿主即便不注入本规范，agent 读工具说明也能维持复利闭环；注入规范用于进一步收紧写入质量。

---

## 2. WorkBuddy ✅

配置在 `~/.workbuddy/mcp.json`（已生效）：

```json
{
  "mcpServers": {
    "compound-memory": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "compound-memory", "compound-memory-server"],
      "env": {
        "COMPOUND_MEMORY_ROOT": "$HOME/.agents/memory",
        "COMPOUND_MEMORY_AGENT_ID": "agent-workbuddy"
      }
    }
  }
}
```

修改后重启 WorkBuddy 生效。

**使用规则的注入**：把 §6 的「共通使用规范」写进用户级记忆文件 **`~/.workbuddy/MEMORY.md`**（已落地）。注意两个细节：

- 每会话真正自动注入的是它的镜像 `~/.workbuddy/user-<uid>-personal/MEMORY.md`，**两份要同步改**，否则规则不生效；
- WorkBuddy 没有 SessionStart 注入机制，规则里显式保留「任务开始先 `memory_search`」这一步——不像 ZCode 有 hook 兜底召回，这里不能省。

验证：让 Agent 调一次 `memory_search`（如查询 "compound-memory"），返回 `{"hits": [...]}` 即工作正常；`memory_feedback` 的 `agent` 参数固定填 `agent-workbuddy`。

## 3. ZCode ✅

配置在 `~/.zcode/cli/config.json`，两段（均已生效）：

**MCP server**：

```json
{
  "mcp": {
    "servers": {
      "compound-memory": {
        "type": "stdio",
        "command": "uvx",
        "args": ["--from", "compound-memory", "compound-memory-server"],
        "env": {
          "COMPOUND_MEMORY_ROOT": "$HOME/.agents/memory",
          "COMPOUND_MEMORY_AGENT_ID": "agent-zcode"
        }
      }
    }
  }
}
```

**SessionStart hook 自动注入**（会话启动时把 `_shared` 记忆以 additionalContext 塞进上下文，最多 20 条、每条 300 字符、按置信度+新近排序、跳过过期 TTL；脚本在 `~/.agents/memory/hooks/session_start.py`）：

```json
{
  "hooks": {
    "enabled": true,
    "events": {
      "SessionStart": [
        {
          "hooks": [
            {
              "type": "process",
              "command": "~/.workbuddy/binaries/python/envs/default/bin/python",
              "args": ["~/.agents/memory/hooks/session_start.py"],
              "timeoutMs": 10000,
              "statusMessage": "Loading compound-memory"
            }
          ]
        }
      ]
    }
  }
}
```

hook 只做**召回**；**写入与 feedback** 仍靠 Agent 主动调用工具，行为规则写在 `~/.zcode/AGENTS.md`（「compound-memory 记忆使用规则」一节）。有 SessionStart 注入后，任务开始前的 `memory_search` 可酌情省略——注入内容已覆盖大部分高频记忆，但精确查询仍应 search。

## 4. Claude Code ✅

两种方式任选其一：

**方式 A：CLI 一条命令（推荐）**

```bash
claude mcp add compound-memory \
  -e COMPOUND_MEMORY_ROOT=$HOME/.agents/memory \
  -e COMPOUND_MEMORY_AGENT_ID=agent-claude \
  -- uvx --from compound-memory compound-memory-server
```

加 `-s user` 写入用户级（全局可用），不加则默认 local（仅当前项目）。

**方式 B：手写 JSON**。项目级放仓库根 `.mcp.json`（随 git 分享给协作者），用户级放 `~/.claude.json` 的 `mcpServers` 键：

```json
{
  "mcpServers": {
    "compound-memory": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "compound-memory", "compound-memory-server"],
      "env": {
        "COMPOUND_MEMORY_ROOT": "$HOME/.agents/memory",
        "COMPOUND_MEMORY_AGENT_ID": "agent-claude"
      }
    }
  }
}
```

**使用规则注入**：把 §6 的规范写进用户级 `~/.claude/CLAUDE.md`（或项目 `CLAUDE.md`）。Claude Code 没有 SessionStart 注入机制，任务开始先 `memory_search` 尤其重要。

验证：`claude mcp list` 应显示 `compound-memory: connected`。上面的接入一览与本节同为 ✅：配置与使用规则注入方式已经落地，不是待办。

## 4b. Cursor

Copy-paste setup. `COMPOUND_MEMORY_AGENT_ID` is required (a stable id such as `agent-cursor`). Without it, any caller can claim another host's namespace, and `stats` / `doctor` exit 1.

Project config is `.cursor/mcp.json`; user-level is `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "compound-memory": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "compound-memory", "compound-memory-server"],
      "env": {
        "COMPOUND_MEMORY_ROOT": "$HOME/.agents/memory",
        "COMPOUND_MEMORY_AGENT_ID": "agent-cursor"
      }
    }
  }
}
```

Restart Cursor after saving the file. A `memory_search` result of `{"hits": [...], "retrieval": {...}}` means the server is connected. If `retrieval.mode` is `lexical`, vector recall is off (missing `vec` extra or model); results are BM25 only. A local checkout is the developer path in §1, not a second default: `uv` plus `["run", "--directory", "<absolute-repo-path>", "compound-memory-server"]` after `uv sync --extra dev`.

Three protocol rules (same as §6; point a Cursor rule at `skills/compound-memory/SKILL.md` so they stay in the session):

1. On a non-trivial task, `memory_search` before acting.
2. After actually using a hit, `memory_feedback` with your agent id (`success` when it helped, `failure` / `contradiction` / `obsolete` / `unknown` when it did not).
3. At the end of the session, `memory_write` only stable facts (preferences, conventions, environment constraints, pitfalls) — not scratch notes. `content` over 262144 characters is rejected.

## 5. DeepSeek Harness (dsh) ✅

DSH 接入外部记忆的官方路径是宿主自带的 `@deepseek-ai/dsh-mcp-client`（stdio 或 Streamable HTTP）。官方的第三方记忆示例也是同一行配置，没有另给一个能直接挂上这条 PyPI server 的 memory-provider 插槽。在 JS 里再注册一套记忆工具等于重做存储。所以这里用配置层 bundle 插入一行 stdio，server 仍是 `uvx --from compound-memory compound-memory-server`，MCP tool 仍是那 5 个。模型侧名字带宿主前缀：`mcp__compound-memory__memory_search`（以及 get / write / link / feedback）。

`integrations/dsh` 按未来公开仓库 `chinwe/compound-memory-dsh` 的**根**来铺（该仓库尚未创建，不在本仓库里建）：根上的 `package.json` 声明 `dsh.bundle.patch` = `./cordis.patch.yml`，同目录有 `skills/compound-memory/SKILL.md` 与 README。可以整目录原样复制成那个仓库的根。npm 包名 `dsh-compound-memory`。bundle 只插入一行 MCP client，并带上 skill 文件；没有 install / lifecycle `scripts`。

**安装 bundle**：

```bash
dsh plugin --profile web add <仓库绝对路径>/integrations/dsh
```

公开仓库建好并把该目录作为根推上去之后（没有 `#path`，也不需要 `prepare` / `allowBuilds`）：

```bash
dsh plugin --profile web add github:chinwe/compound-memory-dsh
```

npm 发布之后（发布是手工步骤，本仓库不自动做）：

```bash
dsh plugin --profile web add dsh-compound-memory
```

`dsh plugin` 把该包写入 profile 的 `dsh.profile.bundles`。bundle 没有自己的插件模块。

在**启动 dsh 的进程**里设置环境变量。patch 用 `!!js process.env...` 读取；未设置或空白时 `COMPOUND_MEMORY_ROOT` 默认为 `$HOME/.agents/memory`（Python 展开 `$HOME`），`COMPOUND_MEMORY_AGENT_ID` 默认为 `agent-dsh`（与 §1 的宿主 id 相同）。不要在 patch 里写密钥。

`uvx` 必须在 dsh 的 `PATH` 上。后一层 patch 按 id **整行替换** `config`，不是深合并。要改成绝对路径时，在 `~/.dsh/profiles/web/cordis.patch.yml` 重写 `mcp-compound-memory` 这一行的全部字段，`command` 写成 `uvx` 的绝对路径。

验证：`dsh --profile web --dump-config` 里有 `# == dsh-compound-memory` 层。启动后五个工具注册为 `mcp__compound-memory__memory_*`。`memory_search` 的返回体含 `{"hits": [...]}`。

**不装 bundle、手写同一行**时，写进 `~/.dsh/profiles/web/cordis.patch.yml`。生成文件里的 `[]` 要换成下面这块，不要在 `[]` 下面再追加（那样 YAML 非法，dsh 起不来）：

```yaml
- insert:
    - id: mcp-compound-memory
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: compound-memory
        transport: stdio
        command: uvx
        args: ['--from', 'compound-memory', 'compound-memory-server']
        env:
          COMPOUND_MEMORY_ROOT: !!js "process.env.COMPOUND_MEMORY_ROOT || '$HOME/.agents/memory'"
          COMPOUND_MEMORY_AGENT_ID: !!js "process.env.COMPOUND_MEMORY_AGENT_ID || 'agent-dsh'"
        failOnStartupError: true
```

要点：

- `serverName` 是工具命名空间（`mcp__compound-memory__memory_search`），`[A-Za-z0-9_-]{1,32}` 且同一注册域内唯一；
- `failOnStartupError: true` 让这一行在 `uvx` 或 server 起不来时拒绝激活（调试期保持开着）；
- 工具调用默认超时 60s（`toolCallTimeoutMs` 可调），本 server 全部为本地文件操作，不会触及。

**使用规则注入**：随包 skill 在 `integrations/dsh/skills/compound-memory/SKILL.md`（正文与仓库 `skills/compound-memory/SKILL.md` 同一套规范，开头多一段 DSH 安装说明）。DSH 不扫描 npm 包内的 `skills/`，只扫描 skill 根的直接子项（`~/.dsh/skills/<name>/SKILL.md`、项目 `.dsh/skills`、`~/.agents/skills`、项目 `.agents/skills`）。本包没有安装脚本，不会在安装时复制。要让 skill 工具加载它，把 `skills/compound-memory` 复制或软链到其中一个根。规则也可以写进对应 preset 的 agent 指令文件（Standard preset 的 system prompt 位置随版本变化，以 `~/.dsh/profiles/` 下实际 preset 为准），内容引用 §6。`source` / `agent` 填 `agent-dsh`，除非 `COMPOUND_MEMORY_AGENT_ID` 被覆盖成别的 id。英文安装步骤见 `integrations/dsh/README.md`。

---

## 6. 共通使用规范（注入给各 Agent 的行为规则）

**规范源是 `skills/compound-memory/SKILL.md`**（随 skill 分发到各宿主，更新只改那一处）；本节保留为速览快照，两处不一致时以 skill 为准。新宿主接入优先分发该 skill，规则注入文件里只保留触发铁律（任务开始先 `memory_search`、采纳后 `memory_feedback`）加一行指向 skill 的指针。

以下为速览内容，可直接复制：

1. **任务开始**：接到非琐碎任务时，先按任务关键词 `memory_search` 查相关记忆（用户偏好、项目背景、环境坑）；命中且实际采纳后**必须**调 `memory_feedback`（`agent` 填自己的 source id）强化——这是复利闭环的核心动作，漏掉它记忆库就不增值。
2. **任务结束**：会话中确认了稳定事实（新的用户偏好、项目约定、环境限制、踩坑结论），用 `memory_write` 沉淀。`type` 怎么选：
   - `fact`：客观事实（配置、账号、环境参数）——带稳定 `key`，同 key 新版本会触发冲突复核；
   - `insight`：经验教训（怎么绕坑、什么方案有效）——带 `key`；稳态 lesson 通常用这个 type，并标 `lesson_role: lesson`；
   - `skill`：可复用的操作方法；
   - `decision`：已做的选择（选型、方案拍板）——带 `key`，同 key 新决策触发冲突复核；avoid 用这个 type，并标 `lesson_role: avoid`；
   - `episode`：事件经历（部署了什么、发生了什么）。
3. **不写**：一次性、会话内临时信息；记忆内容用中文，与库内既有条目保持一致；更新既有事实优先复用同 `key` 而非新开一条。
4. **关联**：新记忆与已有记忆有因果/派生关系时用 `memory_link` 连上（必须同 ns；私有 ns 记忆带 `agent` 填自己的 source id），检索时邻居会被自动带出。
5. **邻居是线索不是结论**：search/get 返回的 `neighbors` 只做上下文参考，采纳哪条以 hit 本身为准。

## 7. 运维与蒸馏（CLI，所有宿主共用）

日常没有仓库目录，前缀是 `uvx --from compound-memory compound-memory`（已 `uv tool install compound-memory` 时可直接写 `compound-memory`）。从仓库联调时在克隆目录改回 `uv run compound-memory`（见 §1）。

```bash
uvx --from compound-memory compound-memory doctor         # 一页健康诊断（安装/多宿主自检）：
                                      #   attestation（AGENT_ID 是否注入）、retrieval（vector/lexical + reason）、
                                      #   write_lock（能否加独占锁）、git（是否仓库、工作树是否脏）
                                      #   退出码：0 健康；1 硬失败（AGENT_ID 未设置或空白、根目录缺失、
                                      #   写锁不可用、git 仓库不可用）。词面降级与脏工作树只报告、不因此失败。
                                      #   stdout 是 JSON，告警在 stderr。不创建缺失根目录，不 git init。
                                      #   不新增 MCP 工具。调用方参数错误仍是退出码 2
uvx --from compound-memory compound-memory stats          # 健康度：uses/confidence 固定桶 + 活性 + 蒸馏产出量
                                      #   + retrieval（vector/lexical）+ attestation（进程身份是否注入）
                                      #   + expired_active（活动区里 valid_until 已过）
                                      #   + cross_validated（validated_by 去重后 ≥2 的条数，
                                      #     不是「有过一次跨宿主反馈」）
                                      #   AGENT_ID 未设置或空白时退出码 1（JSON 仍打印）；已设置时为 0
uvx --from compound-memory compound-memory explain <id> [--reader <agent id>]  # 按 id 证据视图（#44）：证据计数
                                      #   （success/failure/contradiction、last_verified、recent 明细）、
                                      #   跨宿主验证明细（validated_by）、派生标记（蒸馏产物零证据起点）、
                                      #   当前 conf；只读不产生提交；私有 agent-* ns 加 --reader
uvx --from compound-memory compound-memory decay          # 衰减归档（launchd/cron 定时跑；长期未用且少用才动）
uvx --from compound-memory compound-memory revive <id>    # 复活归档记忆（CLI 唯一入口）
uvx --from compound-memory compound-memory forget <id> --agent <agent id> [--reason <动机短语>]  # 终态遗忘（ADR-0009）：
                                      #   文件物理移出（活动/归档区皆可）+ 单条 commit 留痕，内容仅存 git 历史；
                                      #   不可复活（对被遗忘记忆 feedback/revive 返回 found: false，恢复 = 带外 git 运维）；
                                      #   私有 agent-* ns 仅属主可遗忘（--agent 与 feedback 同规）；幂等（不存在/已遗忘
                                      #   返回 found: false）；--reason 是动机短语（单行限 80 字符），不贴记忆正文；
                                      #   隐私边界：解决「活动库不再携带」，不解决「历史不再包含」
uvx --from compound-memory compound-memory review-queue   # fact/insight/decision 同 key 冲突队列（人工复核，CLI 唯一入口；展示全量）
uvx --from compound-memory compound-memory review-resolve <废置id> [--reader <agent id>]  # 清行并自动归档废置方；
                                      #   私有 agent-* ns 的行仅属主可清（--reader）；--all 只清行不归档，
                                      #   且对非属主的私有行静默保留；contradiction 争议行加 --uphold
                                      #   表示维持原记忆（解冻并按 failure −0.2 折算），缺省为确错归档
uvx --from compound-memory compound-memory rebuild-index  # 手编已有文件内容后重建检索缓存
uvx --from compound-memory compound-memory extract <transcript|dir>  # 会话抽取清单（P0）：确定性扫描 →
                                      #   extract/last-candidates.json；Agent 逐条确认后 memory_write 落库
                                      #   transcript 按内容自动判别四种形态（WorkBuddy session log /
                                      #   ZCode 会话库 sqlite / Claude Code session log / dsh zstd session）；
                                      #   传目录批量扫：extract ~/.workbuddy/projects 或 ~/.claude/projects
                                      #   或 ~/.dsh/sessions（自动跳过 subagents/ 与 dsh subagent 子会话）
                                      #   ZCode 直接指库文件：extract ~/.zcode/cli/db/db.sqlite（只读打开）
                                      #   注意 traces/ 与 rollout/model-io 快照不接入（都只剩部分轮次，是假阴性）
uvx --from compound-memory compound-memory git-log        # 审计轨迹（每次写入自动 commit）
                                      #   消费端降噪（#31）：--grep PATTERN 只留消息匹配的提交（可多次，OR）、
                                      #   --exclude PATTERN 剔除匹配的提交（可多次）；PATTERN 为正则，作用于
                                      #   消息段（剥掉 hash），过滤发生在 --limit 取数之后，
                                      #   如 git-log --exclude feedback
```

**蒸馏**（把一批旧记忆沉淀为更高密度产物，判断归调用方 Agent）：

```bash
uvx --from compound-memory compound-memory dream --ns _shared   # 只读编排（ADR-0001，无 LLM）：
                                      #   distill 段 = distill-plan（merge_with / possible_dup_of /
                                      #   promotion_candidate / key_duplicates / suggested_action /
                                      #   negative_salience / contradiction_pairs）；
                                      #   suggested_action 只有 merge / promote / review；
                                      #   负向段与矛盾对只报告，不放宽 dream-apply；
                                      #   decay_signals（expired_count / archived_count / low_activity_count）；
                                      #   stats_before 与 stats_after 空跑相等。不归档、不 distill-apply、不 forget
uvx --from compound-memory compound-memory distill-plan   # 只要候选清单时：信号 + suggested_action
                                      #   + negative_salience + contradiction_pairs
# Agent 读报告：merge（同 key 簇，source_ids 已列出；跨 key 综合也叫 merge，不叫 synthesize）
# → 自己写正文 → distill-apply 或 dream-apply。promote 须显式 promote: true。
# review（possible_dup_of，含跨 key 弱相似）不要放进 dream-apply。没有 supersedes: 字段。
# 半自动落库：proposals.json 是列表，每项自带 content / type / source_ids
# （晋升项加 promote: true）。先 --dry-run，确认 planned / skipped 后再去掉。
uvx --from compound-memory compound-memory dream-apply --proposals proposals.json --source agent-zcode --dry-run
                                      # 只放行 merge_with / key_duplicates / 点名的 promotion_candidate，
                                      # 经既有 distill-apply 落库；possible_dup_of 只报告。定时任务不要调用
uvx --from compound-memory compound-memory distill-apply "合并后的经验" insight agent-zcode \
  --sources <id1>,<id2>                    # 单条原子落库：产物 links 溯源 + 源归档（可复活）
```

**教训（lesson / avoid）**。借用教训语义，不搬因果引擎。不新增记忆类型，不新增 MCP 工具。

| 时机 | 动作 |
|---|---|
| 写教训 | 只写稳态。lesson 多用 `insight`（稳态可用 `decision`），`lesson_role: lesson`。avoid 用 `decision` + `lesson_role: avoid`。不要把角色写进 key。并排时 links 用 `avoid:` / `fixes:` / `contradicts:` 加目标 id 或 key |
| 动手前 | `memory_search` 之后对命中 `memory_get`，看 `lesson_role`，并顺着 `avoid:` 链接再 get（前缀不会被邻居召回自动展开） |
| 读报告 | 读 `dream` / `distill-plan` 的 `negative_salience` 与 `contradiction_pairs`（dream 在 `distill` 段）。只读；`suggested_action` 仍只有 merge / promote / review |
| 失败反馈 | 教训误导了你就 `memory_feedback`，`outcome: failure` |
| 错教训作废 | 新写纠正正文 + 归档源，走既有 `memory_write` / `distill-apply`。负向段与矛盾对不放宽 `dream-apply` |

launchd（macOS）/ systemd user timer（Linux）/ cron 每天 09:00 自动跑只读 `dream`，把报告写到 `<root>/distill/last-plan.json`（`distill` 段即候选清单，含负向段与矛盾对；安装物与命令见 README「定时蒸馏准备」）。定时脚本不调用 `dream-apply`。各宿主 Agent 任意会话中发现报告有新候选时按需处理即可。

## 8. 故障排查

| 症状 | 处置 |
|---|---|
| 宿主里看不到 5 个 memory_* 工具 | 先手动跑启动命令看报错：`uvx --from compound-memory compound-memory-server`。多为 uvx 不在 PATH：`command` 改成 `~/.local/bin/uvx` 的绝对路径。从仓库联调才用 `uv run --directory <仓库绝对路径> compound-memory-server`；那种配置在仓库移动后要同步改 `--directory` |
| 搜索结果为空 / 召回不全 | `stats` 看记忆量与 `retrieval`；`mode=lexical` 表示向量路没开或当次降级，排序是词面。怀疑索引损坏时 `rebuild-index`（缓存可随时重建，检索降级不报错，但 `retrieval.reason` 会标明） |
| 检索排序不符合预期 / 想知道某条为什么排前 | `memory_search` 传 `explain: true`（CLI `--explain`）看每条 hit 的排序分量（词面/向量 rank、RRF 分、先验折算项、检索通道）与证据摘要；单条记忆的置信度构成用 CLI `explain <id>`（证据计数、跨宿主验证明细、派生标记） |
| server 日志出现 `vector recall degraded to lexical` | 向量召回故障已自动降级纯词面（检索不中断）；多为 vec extra 环境或向量索引异常，重装 `--extra vec` 或 `rebuild-index`；未装 vec extra 的宿主不会出现此日志 |
| server 日志出现 `write lock unavailable`，或写入报 `refusing unlocked write` | 前者：`.lock` 文件创建失败（异常文件系统），已降级无锁写入。后者：锁争用超时或加锁系统调用失败，**写入被拒绝**（fail-closed），没有无锁继续；等对方释放后重试。Windows 等待上界 30 秒 |
| server 日志出现 `skipping unparseable memory file` | 库内有解析失败的坏文件已被扫描跳过并保留原样，按日志路径人工检查/修复该文件 |
| 手工编辑过记忆文件内容 | 活性检测只覆盖新增/删除，**内容**修改需显式 `rebuild-index` |
| `git-log` 出现 `orphan changes recovered` | 不是异常：上次会话 commit 失败/进程中断/带外手编留下的未提交变更，已被启动对账收编进这条恢复提交（消息不声称作者）。看到它值得回查当时是否有写动词报过 git 失败；带外变更想要专属提交历史的，手编后自行在记忆库 git commit |
| SessionStart 没注入 | hook 任何异常都静默退出；手动跑 `~/.agents/memory/hooks/session_start.py` 检查输出是否为合法 `{"additionalContext": ...}` JSON |
| 写入报 PermissionError | 命名空间越权：`agent-*` 私有区仅属主可写，日常写 `_shared` |
| 写入/检索报 ValueError（`ns contains characters...`） | ns 含白名单外字符（`../`、`/`、`*`、空格等）：ns 是存储路径组件，只允许 `[A-Za-z0-9_-]`，私有区写 `agent-<宿主 id>` |
| 项目记忆检索不到 / 报 `project must match` | 检索缺省 fail-closed 只见全局记忆：核对调用方 `project` 参数（或 CLI `COMPOUND_MEMORY_PROJECT`）与记忆标注一致；slug 格式与 key 相同（小写字母数字段短横线连接，禁大写/下划线/空格）；按 id `memory_get` 恒可读，可先取回核对 frontmatter |
| `stats` / `doctor` 退出码 1，或 `stats.attestation.warning` / 启动日志提到 `COMPOUND_MEMORY_AGENT_ID is unset` | 宿主 MCP env 没注入进程身份（未设置或空白）。多 Agent 共享库必须补上稳定的 `COMPOUND_MEMORY_AGENT_ID` 后重启 server。CLI 健康检查以此为硬失败；MCP 工具调用不因此中断，但未注入时调用方可以冒充其他 agent |
| `doctor` 报 `root` / `write_lock` / `git` | 根目录不存在先 `init`。写锁加不上：另一进程持锁，或锁文件创建失败。git 不可用：git 不在 PATH、记忆库还不是仓库、或 `git status` 失败。脏工作树只提示，启动对账会收编 |
| 写入报 `content exceeds` | 正文超过 262144 字符，本次没有落盘。改成短事实或只存指针 |
| 同 key 写入返回 `conflict: true` | 内容与既有版本不同，已入 `review-queue.md`。裁决后跑 `review-resolve <废置id>`：清行并自动归档废置方（索引同步、一次 commit）。不要手改 frontmatter 再搬文件——那会绕过清行与索引收口。`--all` 只清行不归档；私有 `agent-*` 行加 `--reader` |
| 记忆被归档了 | 按 id `memory_get` 可取回；对它 `memory_feedback`（obsolete 除外）或 `revive` 即恢复可检索。`forget` 之后文件已不在，get / feedback / revive 都是 `found: false`，恢复只能走记忆库 git |
