# compound-memory

[English](README.md) | 简体中文

本地多 Agent 共享记忆系统——支持复利（越用越值钱）。记忆以带 frontmatter 的 Markdown 落盘，随使用增值，长期不用则衰减归档、可复活。

**快速上手**见下；接入配置与统一使用规范见 `docs/agent-integration.md`；设计 Spec 见 `docs/specs/0001-compound-memory-spec.md`。

## 快速上手

### 给 Agent 的一句话集成

把下面这句提示词粘贴给你的编码 Agent（Claude Code / Cursor / ZCode 等），它就能完成全部接入：

```text
在这台机器上安装并接入 compound-memory（https://github.com/chinwe/compound-memory）——本地优先的多 Agent 共享记忆系统（MCP server + CLI）：安装（`uv tool install compound-memory`，或克隆仓库后 `uv sync --extra dev`）、初始化记忆库（`compound-memory init`，默认 ~/.agents/memory）、在本宿主的 MCP 配置里注册 stdio server——命令 `compound-memory-server`（PyPI 安装）或 `uvx --from compound-memory compound-memory-server`，env 设 `COMPOUND_MEMORY_ROOT=$HOME/.agents/memory` 和 `COMPOUND_MEMORY_AGENT_ID=agent-<你的宿主标识>`——然后调用 `memory_search` 验证返回 `{"hits": [...]}` 即接入成功；若宿主需重启才能加载 MCP server，请告诉我。各宿主配置细节与使用规范见仓库内 docs/agent-integration.md。
```

### 给人类的步骤

#### 安装（Python ≥ 3.11，二选一）

```bash
# 方式一：源码克隆（uv 管理；MCP 接入同款路径）
git clone https://github.com/chinwe/compound-memory.git
cd compound-memory && uv sync --extra dev

# 方式二：PyPI 安装（无需克隆，MCP server 可直接 uvx 拉起）
uv tool install compound-memory   # 或 pip install compound-memory
```

#### 初始化记忆库

默认 `~/.agents/memory`，可用环境变量 `COMPOUND_MEMORY_ROOT` 覆盖（前缀 `~` 或 `$HOME` 会展开；空白覆盖回退默认路径）：

```bash
uv run compound-memory init
```

#### 接入 MCP 宿主（推荐）

让日常 Agent 自动读写共享记忆——

```json
{
  "mcpServers": {
    "compound-memory": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "<仓库>", "compound-memory-server"],
      "env": {
        "COMPOUND_MEMORY_ROOT": "$HOME/.agents/memory",
        "COMPOUND_MEMORY_AGENT_ID": "agent-<你的宿主标识>"
      }
    }
  }
}
```

PyPI 安装形态把 `command`/`args` 换成 `uvx` + `["--from", "compound-memory", "compound-memory-server"]` 即可，不再依赖仓库克隆路径。`COMPOUND_MEMORY_AGENT_ID` 建议必配：注入后存储层以进程身份裁决一切自报身份，模型谎报身份、伪造 source 污染跨 Agent 验证的通道一并关闭。

**验证**：在宿主里让 Agent 调用 `memory_search`（如查询任意关键词），能返回 `{"hits": [...]}` 即接入成功；或直接跑 CLI `uv run compound-memory stats`。

#### 下一步

把 `skills/compound-memory/SKILL.md` 的使用规范注入宿主（检索 → 反馈 → 沉淀的复利闭环），详见 `docs/agent-integration.md` §6。

## Demo

![compound-memory CLI 演示：init → write → search → feedback → stats](https://raw.githubusercontent.com/chinwe/compound-memory/main/assets/demo.gif)

一次完整闭环：写入 → 检索 → 反馈强化（含跨 Agent 首验加分）→ 健康度。以下输出实录自 v0.4.0（长输出有截断）：

```bash
uv run compound-memory init
```

```json
{ "ok": true, "root": "~/.agents/memory" }
```

两个不同宿主各写一条稳定事实（新记忆 `confidence` 0.5 起步、`uses` 0 起步）：

```bash
uv run compound-memory write \
  "Deploy serverless functions on this platform times out at 10s — keep handlers under that budget" \
  fact agent-claude --key vercel-timeout
```

```json
{
  "id": "20261007_86adf1",
  "ns": "_shared",
  "type": "fact",
  "source": "agent-claude",
  "content": "Deploy serverless functions on this platform times out at 10s — keep handlers under that budget",
  "confidence": 0.5,
  "uses": 0,
  "key": "vercel-timeout",
  "validated_by": []
  ...
}
```

```bash
uv run compound-memory write \
  "User prefers concise replies with tables and code examples" \
  fact agent-zcode --key user-style
```

检索按分数排序（0.4.0 起支持 `--explain` 透出排序分量，排障用）：

```bash
uv run compound-memory search "serverless timeout"
```

```json
[
  {
    "id": "20261007_86adf1", "score": 1.0292, "similarity": 1.0,
    "type": "fact", "source": "agent-claude",
    "content": "Deploy serverless functions on this platform times out at 10s — keep handlers under that budget",
    "neighbors": []
  },
  {
    "id": "20261007_6a0c0c", "score": 0.5211, "similarity": 0.4919,
    "type": "fact", "source": "agent-zcode",
    "content": "User prefers concise replies with tables and code examples",
    "neighbors": []
  }
]
```

另一宿主采纳了这条记忆并回写反馈——`uses` +1、`conf` +0.1；反馈方 `agent-workbuddy` ≠ source `agent-claude`，跨宿主首验再 +0.15：

```bash
uv run compound-memory feedback 20261007_86adf1 agent-workbuddy
```

```json
{
  "id": "20261007_86adf1",
  "confidence": 0.75,
  "uses": 1,
  "last_used": "2026-10-07",
  "validated_by": ["agent-workbuddy"],
  "evidence": {
    "success_count": 1, "failure_count": 0, "contradiction_count": 0,
    "last_verified": "2026-10-07",
    "recent": [{ "date": "2026-10-07", "agent": "agent-workbuddy", "outcome": "success" }]
  }
  ...
}
```

库健康度一览（固定桶直方图 + 活性 + 蒸馏产出）：

```bash
uv run compound-memory stats
```

```json
{
  "total": 2, "archived": 0, "active": 2,
  "avg_confidence": 0.625,
  "by_type": { "fact": 2 },
  "by_ns": { "_shared": 2 },
  "review_queue_entries": 0,
  "uses_histogram": { "0": 1, "1-2": 1, "3-5": 0, "6-9": 0, "10+": 0 },
  "confidence_histogram": { "<0.3": 0, "0.3-0.6": 1, "0.6-0.8": 1, "0.8-1.0": 0 },
  "recent_feedback_7d": 1, "cross_validated": 0,
  "distilled_total": 0, "distilled_recent_7d": 0
}
```

三个看点：

- 每条新记忆 `confidence` 0.5 起步，靠**证据**涨跌（feedback 可带 outcome：success 升 / failure 降 / contradiction 冻结入冲突队列 / obsolete 立即归档）；
- 跨宿主首验独立加分（每宿主每记忆一次），`validated_by` / `evidence` 全程留痕——置信度是证据正确性，不是人气；
- 检索与 `memory_get` 命中自动内嵌一度邻居（本例无关联故为空；`memory_link` 建立双向关联后自动带出）。

## 架构

```
Agent (MCP 客户端 / CLI)
  └─ memory_write | memory_search | memory_get | memory_link | memory_feedback
       └─ MemoryStore (~/.agents/memory)
            ├─ namespaces/_shared/{episode,fact,insight,skill}/*.md   共享区
            ├─ namespaces/agent-*/...                                  私有区
            ├─ archive/...                                             衰减归档（可复活）
            ├─ index/tokens.json                                       可重建的检索缓存
            ├─ review-queue.md                                         fact/insight 冲突队列
            └─ .git/                                                   每次写入自动 commit
```

## 复利机制

| 利息来源 | 实现 |
|---|---|
| ① 使用强化 | `memory_feedback`: uses+1, conf+0.1 |
| ② 关联增值 | `memory_link` 双向关联；`memory_get` 带出一度邻居；`search` 命中自动内嵌精简邻居（上限 3、只召回活动记忆，`--no-neighbors` 可关） |
| ③ 蒸馏提纯 | `distill-plan`（CLI，确定性候选+双信号去重标注）→ Agent 判断 → `distill-apply` 原子落库（产物 links 溯源，源归档可复活） |
| ④ 跨 Agent 验证 | 与 source 不同的 agent 反馈时 conf 额外 +0.15 |

评分公式（权重以 `src/compound_memory/scoring.py` 的 `W_*` 常量为准）：`0.70·相似度 + 0.15·置信度 + 0.10·新近度(0.5+0.5·e^(−Δt/τ)) + 0.05·类型权重`；双路（向量路启用）时改为 RRF 融合主序 + ε=0.04 先验 tie-break（见 spec「索引即缓存」）。

## MCP 接入

各宿主（WorkBuddy / ZCode / Claude Code / DeepSeek Harness）的完整接入配置与统一使用规范见 `docs/agent-integration.md`。5 个 tool 的 description 自带闭环铁律（命中采纳后必须回写 `memory_feedback`、只写稳定事实、复用既有 key），宿主不注入使用规范也能保持复利闭环——注入规范（agent-integration §6）仍推荐，用于收紧写入质量。

```json
{
  "mcpServers": {
    "compound-memory": {
      "type": "stdio",
      "command": "~/.local/bin/uv",
      "args": ["run", "--directory", "<本目录>", "compound-memory-server"],
      "env": { "COMPOUND_MEMORY_ROOT": "$HOME/.agents/memory" }
    }
  }
}
```

各宿主配置若不展开 `~` 占位写法，替换为本机绝对路径即可。向量召回路为可选（`uv sync --extra vec`）：未装 extra 或 HF 缓存缺模型时自动降级纯词面。embedding 模型与维度可经 `COMPOUND_MEMORY_EMBEDDING_MODEL`（默认 `Xenova/bge-small-zh-v1.5`）与 `COMPOUND_MEMORY_EMBEDDING_DIM`（默认 512）覆盖——换模型属运维动作，改后需显式 `rebuild-index`。

## CLI

```bash
uv sync --extra dev              # 首次克隆后初始化 .venv（之后 uv run 自动使用）

uv run compound-memory init               # 初始化空库
uv run compound-memory write "Vercel Serverless 10s 超时" episode agent-workbuddy
uv run compound-memory search "Vercel 超时"     # 命中内嵌一度邻居（上限3，--no-neighbors 关闭）
uv run compound-memory feedback <id> agent-claude
uv run compound-memory decay          # cron 定时跑
uv run compound-memory revive <id>    # 复活归档记忆（CLI 唯一入口）
uv run compound-memory distill-plan   # 蒸馏候选清单：merge_with（同 key 强信号）+ possible_dup_of（BM25 弱信号）+ promotion_candidate（高活性 episode）
uv run compound-memory distill-apply "合并后的经验" insight agent-workbuddy --sources <id1>,<id2>  # 原子落库：产物(links 溯源, origin=distillation) + 源归档，一次 commit
uv run compound-memory stats            # 健康度：uses/confidence 固定桶 + 活性 + 蒸馏产出量
uv run compound-memory rebuild-index  # 索引可随时重建
uv run compound-memory review-queue   # 冲突队列（CLI 唯一入口）
uv run compound-memory git-log        # 审计轨迹
```

其余运维命令：`explain <id>`（单条置信度构成与证据明细）、`forget <id> --agent <id>`（终态遗忘，ADR-0009）、`review-resolve`（冲突裁决）、`extract <transcript|dir>`（会话确定性抽取候选）。

## 定时蒸馏准备（launchd / cron / systemd）

ADR 0001：确定性准备定时跑，判断（摘要/合并）由 Agent 会话内按需完成。每天 09:00 把候选清单写到 `<root>/distill/last-plan.json`。调度器三选一：**launchd**（macOS 系统标准，睡眠错过的计划唤醒后补跑）、**systemd user timer**（`Persistent=true` 同样补跑）、**cron**（最通用但不补跑错过的计划）。三者都调用同一个平台无关的 `scripts/distill-prepare.sh`。

**launchd（macOS）**：

```bash
REPO=$(pwd); UV="$HOME/.local/bin/uv"   # 项目环境由 uv 管理，脚本内经 UV_BIN 覆盖 launchd PATH
sed -e "s|__REPO__|$REPO|g" -e "s|__UV__|$UV|g" -e "s|__ROOT__|$HOME/.agents/memory|g" \
  scripts/com.compound-memory.distill-prepare.plist.tmpl \
  > ~/Library/LaunchAgents/com.compound-memory.distill-prepare.plist
launchctl load ~/Library/LaunchAgents/com.compound-memory.distill-prepare.plist
launchctl list | grep compound-memory   # 验证已加载；日志在 <root>/distill/prepare.log
```

**systemd user（Linux）**：

```bash
REPO=$(pwd); UV="$HOME/.local/bin/uv"
mkdir -p ~/.config/systemd/user
for f in service timer; do
  sed -e "s|__REPO__|$REPO|g" -e "s|__UV__|$UV|g" -e "s|__ROOT__|$HOME/.agents/memory|g" \
    scripts/compound-memory-distill-prepare.$f.example \
    > ~/.config/systemd/user/compound-memory-distill-prepare.$f
done
systemctl --user daemon-reload
systemctl --user enable --now compound-memory-distill-prepare.timer
systemctl --user list-timers | grep compound-memory   # 验证已加载
```

**cron（其他环境）**：`crontab -e` 加入（sed 填充占位符后）：

```
0 9 * * * UV_BIN=$HOME/.local/bin/uv COMPOUND_MEMORY_ROOT=$HOME/.agents/memory /bin/sh <仓库>/scripts/distill-prepare.sh >> $HOME/.agents/memory/distill/prepare.log 2>&1
```

失败要响亮：脚本 `set -eu`，任何一步失败以非 0 退出（`launchctl list` / `systemctl --user list-units` / cron 邮件可见，日志落 distill/prepare.log）。`distill/` 是运行时产物目录（自动加入库 .gitignore），不产生 commit 噪声——只有 Agent 判断后跑 `distill-apply` 才落一次原子 commit。

## 开发

```bash
uv run pytest tests/ -q     # 全量测试（MCP tool 边界 + 蒸馏 + 生命周期/索引/CLI + 输入防御）
uv run mypy src/compound_memory/
```

测试缝：MCP tool 边界（`mcp.Client(server)` 内存直连，无子进程）+ 核心模块单测（scoring / index / store 运维面）。CI 在 Python 3.11/3.12/3.13 矩阵上跑测试、类型检查与纯 wheel 安装冒烟。

## 发布

PyPI 版本不可重传，tag 必须与 `pyproject.toml` 的 `version` 一致（release workflow 有校验，不一致响亮失败）。发布走 GitHub Actions + PyPI Trusted Publisher（OIDC，免 token）：

1. **一次性配置**（PyPI → 项目 → Publishing）：owner `chinwe`、repo `compound-memory`、workflow `release.yml`、environment `pypi`。首次发布时项目尚不存在，在 pypi.org 用"pending publisher"预注册即可。
2. **发布**：`git tag v0.1.0 && git push origin v0.1.0` → `release.yml` 自动 build + `uv publish`。
3. 发布后 `uvx --from compound-memory compound-memory-server` 即为通用安装形态（`uvx` 的参数是包名，script 名不同须用 `--from`；MCP 配置里的 `command` 换成 uvx 后不再依赖仓库克隆路径）。
