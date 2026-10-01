# Agent 接入与使用指南

compound-memory 是本地多 Agent 共享记忆库（存储 `~/.agents/memory`）。所有 Agent 宿主通过**同一条 MCP 边界**（恰好 5 个 tool）接入，共用**同一套 CLI** 做运维与蒸馏；本文给出常见宿主的接入配置与统一的使用规范。

> 术语遵循根目录 `CONTEXT.md`；架构与复利机制见 `README.md`。

## 接入一览

| 宿主 | 状态 | MCP 配置位置 | 使用规则注入方式 |
|---|---|---|---|
| WorkBuddy | ✅ 已接入 | `~/.workbuddy/mcp.json` | SOUL.md / 用户指令补充规则 |
| ZCode | ✅ 已接入 | `~/.zcode/cli/config.json` | SessionStart hook 自动注入 + `~/.zcode/AGENTS.md` |
| Claude Code | ⬜ 待接入 | `~/.claude.json` 或项目 `.mcp.json` | `CLAUDE.md` |
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
| `memory_write` | 写入记忆 | `type`: episode/fact/insight/skill；`source`: 写入方 agent id；fact/insight 建议带稳定 `key` |
| `memory_search` | 检索 | 返回 `{"hits": [...]}` 按分数排序；命中自动内嵌最多 3 条一度邻居；`include_neighbors=False` 可关 |
| `memory_get` | 按 id 取回 | 恒含 `found` 键；默认带一度邻居 |
| `memory_link` | 双向关联两条记忆 | 复利来源②：关联带出 |
| `memory_feedback` | 上报"这条记忆被实际采纳了" | uses+1、conf+0.1；**跨 Agent 验证额外 +0.15**；归档记忆被 feedback 自动复活。**采纳后必须调用** |

### 统一约定（各宿主必须一致）

- **`source` agent id**：WorkBuddy → `agent-workbuddy`；ZCode → `agent-zcode`；Claude Code → `agent-claude`；DeepSeek Harness → `agent-deepseek`。id 用宿主标识而非个性化名字（如 TARS），保证稳定不随命名变化；跨 Agent 验证加分依赖 id 互不相同。
- **namespace**：默认写 `_shared`（全体可见）；`agent-<name>` 是私有区，仅属主可写。日常任务一律用默认值即可。
- **写什么**：稳定事实（用户偏好、项目约定、环境限制、踩坑结论）才写；一次性、会话内临时信息不写。内容用中文，key 用稳定英文短横线标识（如 `user-tts`、`proj-xxx`）。

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
        "COMPOUND_MEMORY_ROOT": "~/.agents/memory"
      }
    }
  }
}
```

修改后重启 WorkBuddy 生效。使用规则的注入：在 WorkBuddy 助理的系统文件（`~/.workbuddy/SOUL.md` 或用户级指令）中补充「共通使用规范」（见 §6）的行为要求即可。

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
          "COMPOUND_MEMORY_ROOT": "~/.agents/memory"
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
        "COMPOUND_MEMORY_ROOT": "~/.agents/memory"
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

以下即各宿主「使用规则注入」的标准内容，可直接复制：

1. **任务开始**：接到非琐碎任务时，先按任务关键词 `memory_search` 查相关记忆（用户偏好、项目背景、环境坑）；命中且实际采纳后**必须**调 `memory_feedback`（`agent` 填自己的 source id）强化——这是复利闭环的核心动作，漏掉它记忆库就不增值。
2. **任务结束**：会话中确认了稳定事实（新的用户偏好、项目约定、环境限制、踩坑结论），用 `memory_write` 沉淀。`type` 怎么选：
   - `fact`：客观事实（配置、账号、环境参数）——带稳定 `key`，同 key 新版本会触发冲突复核；
   - `insight`：经验教训（怎么绕坑、什么方案有效）——带 `key`；
   - `skill`：可复用的操作方法；
   - `episode`：事件经历（部署了什么、发生了什么）。
3. **不写**：一次性、会话内临时信息；记忆内容用中文，与库内既有条目保持一致；更新既有事实优先复用同 `key` 而非新开一条。
4. **关联**：新记忆与已有记忆有因果/派生关系时用 `memory_link` 连上，检索时邻居会被自动带出。
5. **邻居是线索不是结论**：search/get 返回的 `neighbors` 只做上下文参考，采纳哪条以 hit 本身为准。

## 7. 运维与蒸馏（CLI，所有宿主共用）

```bash
uv run compound-memory stats          # 健康度：uses/confidence 固定桶 + 活性 + 蒸馏产出量
uv run compound-memory decay          # 衰减归档（launchd/cron 定时跑；长期未用且少用才动）
uv run compound-memory revive <id>    # 复活归档记忆（CLI 唯一入口）
uv run compound-memory review-queue   # fact/insight 同 key 冲突队列（人工复核，CLI 唯一入口）
uv run compound-memory rebuild-index  # 手编已有文件内容后重建检索缓存
uv run compound-memory git-log        # 审计轨迹（每次写入自动 commit）
```

**蒸馏**（把一批旧记忆沉淀为更高密度产物，判断归调用方 Agent）：

```bash
uv run compound-memory distill-plan   # 确定性候选：merge_with / possible_dup_of / promotion_candidate
# Agent 阅读 last-plan.json 做取舍、拟合并文案（判断段）
uv run compound-memory distill-apply "合并后的经验" insight agent-zcode \
  --sources <id1>,<id2>                    # 原子落库：产物 links 溯源 + 源归档（可复活）
```

launchd 每天 09:00 自动把候选清单写到 `<root>/distill/last-plan.json`（见 README「定时蒸馏准备」）。各宿主 Agent 任意会话中发现清单有新候选时按需处理即可。

## 8. 故障排查

| 症状 | 处置 |
|---|---|
| 宿主里看不到 5 个 memory_* 工具 | 先手动跑启动命令看报错：`uv run --directory <仓库> compound-memory-server`；多为 uv 不在预期路径（`command` 要写绝对路径）或 `--directory` 指向的仓库位置漂移（仓库移动后要同步改各宿主配置） |
| 搜索结果为空 / 召回不全 | `stats` 看记忆量；怀疑索引损坏时 `rebuild-index`（缓存可随时重建，检索永远降级不报错） |
| 手工编辑过记忆文件内容 | 活性检测只覆盖新增/删除，**内容**修改需显式 `rebuild-index` |
| SessionStart 没注入 | hook 任何异常都静默退出；手动跑 `~/.agents/memory/hooks/session_start.py` 检查输出是否为合法 `{"additionalContext": ...}` JSON |
| 写入报 PermissionError | 命名空间越权：`agent-*` 私有区仅属主可写，日常写 `_shared` |
| 同 key 写入返回 `conflict: true` | 内容与既有版本不同，已入 `review-queue.md`；裁决（人或主治 Agent）后把废置版本归档：置 frontmatter `archived: true` 移入 `archive/<ns>/<type>/` 并删活动文件（等价 decay 语义），再 `rebuild-index`，记忆库 git 留一条审计 commit |
| 记忆被归档了 | 按 id `memory_get` 可取回；对它 `memory_feedback` 或 `revive` 即恢复可检索 |
