# Agent 接入与使用指南

compound-memory 是本地多 Agent 共享记忆库（存储 `~/.agents/memory`）。所有 Agent 宿主通过**同一条 MCP 边界**（恰好 5 个 tool）接入，共用**同一套 CLI** 做运维与蒸馏；本文给出常见宿主的接入配置与统一的使用规范。

> 术语遵循根目录 `CONTEXT.md`；架构与复利机制见 `README.md`。

## 接入一览

| 宿主 | 状态 | MCP 配置位置 | 使用规则注入方式 |
|---|---|---|---|
| WorkBuddy | ✅ 已接入 | `~/.workbuddy/mcp.json` | `~/.workbuddy/MEMORY.md`（用户级记忆，每会话自动注入） |
| ZCode | ✅ 已接入 | `~/.zcode/cli/config.json` | SessionStart hook 自动注入 + `~/.zcode/AGENTS.md` |
| Claude Code | ✅ 已接入 | `~/.claude.json`（用户级） | `~/.claude/CLAUDE.md` + 分发 skill |
| DeepSeek Harness (dsh) | ⬜ 待接入 | `~/.dsh/profiles/web/cordis.patch.yml` | agent 指令文件 |

---

## 1. 通用接入参数

所有宿主都是 stdio 方式启动同一个 MCP server，只有三个要素。下文示例中 `~` 表示用户主目录，`<仓库>` 为本仓库的克隆位置；宿主配置若不展开这些占位写法，替换为本机绝对路径即可。

| 项 | 值 |
|---|---|
| 启动命令 | `uv run --directory <仓库> compound-memory-server` |
| `COMPOUND_MEMORY_ROOT` | `~/.agents/memory`（可省略，省略即此默认值） |

**运行环境（uv）**：项目由 uv 管理（`pyproject.toml` + `uv.lock`），uv 不在 PATH 时用 `~/.local/bin/uv`。宿主配置的 `command` 写 uv 绝对路径、`args` 带 `--directory <仓库>`，依赖环境由 `uv run` 自管——首次克隆先 `uv sync --extra dev` 建 `.venv`。启动走 console script `compound-memory-server`（uv 自身输出走 stderr，不污染 MCP 的 stdio 协议）。

**首次使用**先初始化空库（已有库可跳过）：

```bash
uv run --directory <仓库> compound-memory init
```

**验证**：在宿主里让 Agent 调用 `memory_search`（如查询 "compound-memory"），能返回 `{"hits": [...]}` 即接入成功。

### 5 个 MCP tool（唯一读写边界）

| Tool | 用途 | 关键点 |
|---|---|---|
| `memory_write` | 写入记忆 | `type`: episode/fact/insight/skill/decision；`source`: 写入方 agent id；fact/insight/decision 建议带稳定 `key`；可选 `valid_from`/`valid_until`（ISO 日期）标注事实有效期——`valid_until` 已过的事实退出检索结果，但 `memory_get` 仍可读；可选 `project`（小写 slug）标注项目作用域——标注后仅同项目会话检索可见，缺省全局 |
| `memory_search` | 检索 | 返回 `{"hits": [...]}` 按分数排序；命中自动内嵌最多 3 条一度邻居；`include_neighbors=False` 可关。不传 `ns` 时双通道检索：`_shared` + 调用方自有私有 ns（身份已知时，私有条目自动带出）；显式传 `ns` 只搜该 ns，查 `agent-*` 时必带 `reader`（自己的 agent id），缺省即拒绝。可选 `project`（小写 slug）：**fail-closed**——不传只见全局记忆，传了见 全局 ∪ 该项目 |
| `memory_get` | 按 id 取回 | 恒含 `found` 键；默认带一度邻居；目标在私有 ns 时必带 `reader`，缺省即拒绝。按 id 恒可读（project 不限制 get 本体）；可选 `project` 只用于邻居带出的适用性过滤（邻居=全局 ∪ 该项目） |
| `memory_link` | 双向关联两条记忆 | 复利来源②：关联带出；两条记忆必须同 ns，跨 ns 链被拒绝；私有 ns 记忆仅属主可连（`agent` 填自己的 agent id） |
| `memory_feedback` | 上报"这条记忆被实际采纳了" | uses+1、conf+0.1；**跨 Agent 验证额外 +0.15**；归档记忆被 feedback 自动复活；私有 ns 记忆仅属主可反馈。**采纳后必须调用** |

### 统一约定（各宿主必须一致）

- **`source` agent id**：WorkBuddy → `agent-workbuddy`；ZCode → `agent-zcode`；Claude Code → `agent-claude`；DeepSeek Harness → `agent-deepseek`。id 用宿主标识而非个性化名字（如 TARS），保证稳定不随命名变化；跨 Agent 验证加分依赖 id 互不相同。
- **namespace**：默认写 `_shared`（全体可见）；`agent-<name>` 是私有区，仅属主可写、读/反馈/关联也须属主身份（`reader`/`agent` 填自己的 agent id，缺省即拒绝）。检索不传 `ns` 时自动并搜自有私有区（双通道）。ns 只允许字符 `[A-Za-z0-9_-]`（ns 会被直接拼进存储路径，含 `../`、`/`、`*` 等一律 ValueError，2026-10-05 审计加固）。日常任务一律用默认值即可。
- **进程身份注入（建议必配）**：宿主配置的 `env` 加 `COMPOUND_MEMORY_AGENT_ID: <本宿主 agent id>`。注入后存储层以进程身份裁决一切自报身份（source/reader/agent）：缺省自动补真值、等价形式（`agent-x`/`x`）归一化、矛盾响亮拒绝——模型谎报身份失效，伪造 source 污染跨 Agent 验证的通道一并关闭。未注入则保持自报身份模式（协作边界，非安全边界）。
- **project 作用域（按需声明）**：项目专属的记忆（该项目才用的约定/配置/踩坑）写入与检索都带 `project=<slug>`（小写短横线 slug，如 `agenthub`）；跨项目通用的事实不标注。检索缺省 **fail-closed**：不传 `project` 只见全局记忆——「没声明项目 = 全局会话」，项目记忆不外溢进一般会话（ADR 0010）。CLI 侧另有 `COMPOUND_MEMORY_PROJECT` env 回退便利通道（adapter 层读，store 自身不读环境变量；MCP server 全局一份、按项目注入无处落地，显式传参是主语义）。`link` 不限 project（同 ns 即可互链）；蒸馏产物继承源的 project；蒸馏/衰减/复活/stats/review 队列保持库级不过滤。
- **写什么**：稳定事实（用户偏好、项目约定、环境限制、踩坑结论）才写；一次性、会话内临时信息不写。任务状态类（进行时/待办）内容易腐：要么带 `valid_until`、要么改写成不含进行时态的稳定事实——过时的状态记忆比没有更糟。内容用中文，key 用稳定英文短横线标识（如 `user-tts`、`proj-xxx`）。

> 这三条约定与「采纳后必须 feedback」铁律已内嵌在 5 个 MCP tool 的 description 里（server.py），宿主即便不注入本规范，agent 读工具说明也能维持复利闭环；注入规范用于进一步收紧写入质量。

---

## 2. WorkBuddy✅

配置在 `~/.workbuddy/mcp.json`（已生效）：

```json
{
  "mcpServers": {
    "compound-memory": {
      "type": "stdio",
      "command": "~/.local/bin/uv",
      "args": ["run", "--directory", "<仓库>", "compound-memory-server"],
      "env": {
        "COMPOUND_MEMORY_ROOT": "~/.agents/memory",
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
        "command": "~/.local/bin/uv",
        "args": ["run", "--directory", "<仓库>", "compound-memory-server"],
        "env": {
          "COMPOUND_MEMORY_ROOT": "~/.agents/memory",
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

## 4. Claude Code ⬜

两种方式任选其一：

**方式 A：CLI 一条命令（推荐）**

```bash
claude mcp add compound-memory \
  -e COMPOUND_MEMORY_ROOT=$HOME/.agents/memory \
  -- ~/.local/bin/uv run --directory <仓库> compound-memory-server
```

加 `-s user` 写入用户级（全局可用），不加则默认 local（仅当前项目）。

**方式 B：手写 JSON**。项目级放仓库根 `.mcp.json`（随 git 分享给协作者），用户级放 `~/.claude.json` 的 `mcpServers` 键：

```json
{
  "mcpServers": {
    "compound-memory": {
      "type": "stdio",
      "command": "~/.local/bin/uv",
      "args": ["run", "--directory", "<仓库>", "compound-memory-server"],
      "env": {
        "COMPOUND_MEMORY_ROOT": "~/.agents/memory",
        "COMPOUND_MEMORY_AGENT_ID": "agent-claude"
      }
    }
  }
}
```

**使用规则注入**：把 §6 的规范写进用户级 `~/.claude/CLAUDE.md`（或项目 `CLAUDE.md`）。Claude Code 没有 SessionStart 注入机制，任务开始先 `memory_search` 尤其重要。

验证：`claude mcp list` 应显示 `compound-memory: connected`。

## 5. DeepSeek Harness (dsh) ⬜

dsh 通过 MCP client 插件 `@deepseek-ai/dsh-mcp-client` 接入，一个插件实例对应一个 server。本地 server 用 stdio，配置写在 profile patch 文件 `~/.dsh/profiles/web/cordis.patch.yml`：

```yaml
- insert:
  - id: mcp-compound-memory
    name: '@deepseek-ai/dsh-mcp-client'
    config:
      serverName: compound-memory
      transport: stdio
      command: ~/.local/bin/uv
      args: ['run', '--directory', '<仓库>', 'compound-memory-server']
      env:
        COMPOUND_MEMORY_ROOT: ~/.agents/memory
        COMPOUND_MEMORY_AGENT_ID: agent-deepseek
      failOnStartupError: true
```

要点：

- `serverName` 是工具命名空间（`mcp__compound-memory__memory_search`），`[A-Za-z0-9_-]{1,32}` 且实例间唯一；
- `failOnStartupError: true` 让启动失败响亮暴露（调试期建议开着，稳定后可关）；
- 工具调用默认超时 60s（`toolCallTimeoutMs` 可调），本 server 全部为本地文件操作，不会触及；
- 重启 `dsh` 后验证：`dsh web --dump-config | grep -A3 mcp`，并确认工具已注册（`memory_write/search/get/link/feedback` 五个）。

**使用规则注入**：dsh 的模型层与工具层解耦，规则写在对应 preset/系统提示的 agent 指令文件里（Standard preset 的 system prompt 位置随版本变化，以 `~/.dsh/profiles/` 下实际 preset 为准），内容同样引用 §6。

---

## 6. 共通使用规范（注入给各 Agent 的行为规则）

**规范源是 `skills/compound-memory/SKILL.md`**（随 skill 分发到各宿主，更新只改那一处）；本节保留为速览快照，两处不一致时以 skill 为准。新宿主接入优先分发该 skill，规则注入文件里只保留触发铁律（任务开始先 `memory_search`、采纳后 `memory_feedback`）加一行指向 skill 的指针。

以下为速览内容，可直接复制：

1. **任务开始**：接到非琐碎任务时，先按任务关键词 `memory_search` 查相关记忆（用户偏好、项目背景、环境坑）；命中且实际采纳后**必须**调 `memory_feedback`（`agent` 填自己的 source id）强化——这是复利闭环的核心动作，漏掉它记忆库就不增值。
2. **任务结束**：会话中确认了稳定事实（新的用户偏好、项目约定、环境限制、踩坑结论），用 `memory_write` 沉淀。`type` 怎么选：
   - `fact`：客观事实（配置、账号、环境参数）——带稳定 `key`，同 key 新版本会触发冲突复核；
   - `insight`：经验教训（怎么绕坑、什么方案有效）——带 `key`；
   - `skill`：可复用的操作方法；
   - `decision`：已做的选择（选型、方案拍板）——带 `key`，同 key 新决策触发冲突复核；
   - `episode`：事件经历（部署了什么、发生了什么）。
3. **不写**：一次性、会话内临时信息；记忆内容用中文，与库内既有条目保持一致；更新既有事实优先复用同 `key` 而非新开一条。
4. **关联**：新记忆与已有记忆有因果/派生关系时用 `memory_link` 连上（必须同 ns；私有 ns 记忆带 `agent` 填自己的 source id），检索时邻居会被自动带出。
5. **邻居是线索不是结论**：search/get 返回的 `neighbors` 只做上下文参考，采纳哪条以 hit 本身为准。

## 7. 运维与蒸馏（CLI，所有宿主共用）

```bash
uv run compound-memory stats          # 健康度：uses/confidence 固定桶 + 活性 + 蒸馏产出量
uv run compound-memory decay          # 衰减归档（launchd/cron 定时跑；长期未用且少用才动）
uv run compound-memory revive <id>    # 复活归档记忆（CLI 唯一入口）
uv run compound-memory forget <id> --agent <agent id> [--reason <动机短语>]  # 终态遗忘（ADR-0009）：
                                      #   文件物理移出（活动/归档区皆可）+ 单条 commit 留痕，内容仅存 git 历史；
                                      #   不可复活（对被遗忘记忆 feedback/revive 返回 found: false，恢复 = 带外 git 运维）；
                                      #   私有 agent-* ns 仅属主可遗忘（--agent 与 feedback 同规）；幂等（不存在/已遗忘
                                      #   返回 found: false）；--reason 是动机短语（单行限 80 字符），不贴记忆正文；
                                      #   隐私边界：解决「活动库不再携带」，不解决「历史不再包含」
uv run compound-memory review-queue   # fact/insight/decision 同 key 冲突队列（人工复核，CLI 唯一入口；展示全量）
uv run compound-memory review-resolve <废置id> [--reader <agent id>]  # 清行并自动归档废置方；
                                      #   私有 agent-* ns 的行仅属主可清（--reader）；--all 只清行不归档，
                                      #   且对非属主的私有行静默保留
uv run compound-memory rebuild-index  # 手编已有文件内容后重建检索缓存
uv run compound-memory extract <transcript|dir>  # 会话抽取清单（P0）：确定性扫描 →
                                      #   extract/last-candidates.json；Agent 逐条确认后 memory_write 落库
                                      #   transcript 按内容自动判别四种形态（WorkBuddy session log /
                                      #   ZCode 会话库 sqlite / Claude Code session log / dsh zstd session）；
                                      #   传目录批量扫：extract ~/.workbuddy/projects 或 ~/.claude/projects
                                      #   或 ~/.dsh/sessions（自动跳过 subagents/ 与 dsh subagent 子会话）
                                      #   ZCode 直接指库文件：extract ~/.zcode/cli/db/db.sqlite（只读打开）
                                      #   注意 traces/ 与 rollout/model-io 快照不接入（都只剩部分轮次，是假阴性）
uv run compound-memory git-log        # 审计轨迹（每次写入自动 commit）
                                      #   消费端降噪（#31）：--grep PATTERN 只留消息匹配的提交（可多次，OR）、
                                      #   --exclude PATTERN 剔除匹配的提交（可多次）；PATTERN 为正则，作用于
                                      #   消息段（剥掉 hash），过滤发生在 --limit 取数之后，
                                      #   如 git-log --exclude feedback
```

**蒸馏**（把一批旧记忆沉淀为更高密度产物，判断归调用方 Agent）：

```bash
uv run compound-memory distill-plan   # 确定性候选：merge_with / possible_dup_of / promotion_candidate
# Agent 阅读 last-plan.json 做取舍、拟合并文案（判断段）
uv run compound-memory distill-apply "合并后的经验" insight agent-zcode \
  --sources <id1>,<id2>                    # 原子落库：产物 links 溯源 + 源归档（可复活）
```

launchd（macOS）/ systemd user timer（Linux）/ cron 每天 09:00 自动把候选清单写到 `<root>/distill/last-plan.json`（安装物与命令见 README「定时蒸馏准备」）。各宿主 Agent 任意会话中发现清单有新候选时按需处理即可。

## 8. 故障排查

| 症状 | 处置 |
|---|---|
| 宿主里看不到 5 个 memory_* 工具 | 先手动跑启动命令看报错：`uv run --directory <仓库> compound-memory-server`；多为 uv 不在预期路径（`command` 要写绝对路径）或 `--directory` 指向的仓库位置漂移（仓库移动后要同步改各宿主配置） |
| 搜索结果为空 / 召回不全 | `stats` 看记忆量；怀疑索引损坏时 `rebuild-index`（缓存可随时重建，检索永远降级不报错） |
| server 日志出现 `vector recall degraded to lexical` | 向量召回故障已自动降级纯词面（检索不中断）；多为 vec extra 环境或向量索引异常，重装 `--extra vec` 或 `rebuild-index`；未装 vec extra 的宿主不会出现此日志 |
| server 日志出现 `write lock unavailable` 或 `skipping unparseable memory file` | 前者：root 上 `.lock` 无法加锁（异常文件系统），已降级无锁写入，避免多宿主并发写；后者：库内有解析失败的坏文件已被扫描跳过并保留原样，按日志路径人工检查/修复该文件 |
| 手工编辑过记忆文件内容 | 活性检测只覆盖新增/删除，**内容**修改需显式 `rebuild-index` |
| `git-log` 出现 `orphan changes recovered` | 不是异常：上次会话 commit 失败/进程中断/带外手编留下的未提交变更，已被启动对账收编进这条恢复提交（消息不声称作者）。看到它值得回查当时是否有写动词报过 git 失败；带外变更想要专属提交历史的，手编后自行在记忆库 git commit |
| SessionStart 没注入 | hook 任何异常都静默退出；手动跑 `~/.agents/memory/hooks/session_start.py` 检查输出是否为合法 `{"additionalContext": ...}` JSON |
| 写入报 PermissionError | 命名空间越权：`agent-*` 私有区仅属主可写，日常写 `_shared` |
| 写入/检索报 ValueError（`ns contains characters...`） | ns 含白名单外字符（`../`、`/`、`*`、空格等）：ns 是存储路径组件，只允许 `[A-Za-z0-9_-]`，私有区写 `agent-<宿主 id>` |
| 项目记忆检索不到 / 报 `project must match` | 检索缺省 fail-closed 只见全局记忆：核对调用方 `project` 参数（或 CLI `COMPOUND_MEMORY_PROJECT`）与记忆标注一致；slug 格式与 key 相同（小写字母数字段短横线连接，禁大写/下划线/空格）；按 id `memory_get` 恒可读，可先取回核对 frontmatter |
| 同 key 写入返回 `conflict: true` | 内容与既有版本不同，已入 `review-queue.md`；裁决（人或主治 Agent）后把废置版本归档：置 frontmatter `archived: true` 移入 `archive/<ns>/<type>/` 并删活动文件（等价 decay 语义），再 `rebuild-index`，记忆库 git 留一条审计 commit |
| 记忆被归档了 | 按 id `memory_get` 可取回；对它 `memory_feedback` 或 `revive` 即恢复可检索 |
