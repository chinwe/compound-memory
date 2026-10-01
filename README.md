# compound-memory

本地多 Agent 共享记忆系统——支持复利（越用越值钱）。Spec 见 `docs/specs/0001-compound-memory-spec.md`。

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

评分公式：`0.45·相似度 + 0.25·置信度 + 0.20·新近度(e^(-Δt/τ)) + 0.10·类型权重`

## MCP 接入

各宿主（WorkBuddy / ZCode / Claude Code / DeepSeek Harness）的完整接入配置与统一使用规范见 `docs/agent-integration.md`。

```json
{
  "mcpServers": {
    "compound-memory": {
      "command": "python3",
      "args": ["-m", "compound_memory.server"],
      "env": { "PYTHONPATH": "<本目录>/src" }
    }
  }
}
```

## CLI

```bash
PY=python3
export PYTHONPATH=$(pwd)/src

$PY -m compound_memory.cli init               # 初始化空库
$PY -m compound_memory.cli write "Vercel Serverless 10s 超时" episode agent-workbuddy
$PY -m compound_memory.cli search "Vercel 超时"     # 命中内嵌一度邻居（上限3，--no-neighbors 关闭）
$PY -m compound_memory.cli feedback <id> agent-claude
$PY -m compound_memory.cli decay          # cron 定时跑
$PY -m compound_memory.cli revive <id>    # 复活归档记忆（CLI 唯一入口）
$PY -m compound_memory.cli distill-plan   # 蒸馏候选清单：merge_with（同 key 强信号）+ possible_dup_of（BM25 弱信号）+ promotion_candidate（高活性 episode）
$PY -m compound_memory.cli distill-apply "合并后的经验" insight agent-workbuddy --sources <id1>,<id2>  # 原子落库：产物(links 溯源, origin=distillation) + 源归档，一次 commit
$PY -m compound_memory.cli stats            # 健康度：uses/confidence 固定桶 + 活性 + 蒸馏产出量
$PY -m compound_memory.cli rebuild-index  # 索引可随时重建
$PY -m compound_memory.cli review-queue   # 冲突队列（CLI 唯一入口）
$PY -m compound_memory.cli git-log        # 审计轨迹
```

## 定时蒸馏准备（launchd）

ADR 0001：确定性准备定时跑，判断（摘要/合并）由 Agent 会话内按需完成。每天 09:00 把候选清单写到 `<root>/distill/last-plan.json`。调度选 launchd 而非 cron：macOS 系统标准，睡眠错过的计划唤醒后补跑。

```bash
REPO=$(pwd); PY=$(which python3)   # 解释器须已安装 pyyaml
sed -e "s|__REPO__|$REPO|g" -e "s|__PYTHON__|$PY|g" -e "s|__ROOT__|$HOME/.agents/memory|g" \
  scripts/com.compound-memory.distill-prepare.plist.tmpl \
  > ~/Library/LaunchAgents/com.compound-memory.distill-prepare.plist
launchctl load ~/Library/LaunchAgents/com.compound-memory.distill-prepare.plist
launchctl list | grep compound-memory   # 验证已加载；日志在 <root>/distill/prepare.log
```

失败要响亮：脚本 `set -eu`，任何一步失败以非 0 退出（`launchctl list` 可见退出码，日志落 distill/prepare.log）。`distill/` 是运行时产物目录（自动加入库 .gitignore），不产生 commit 噪声——只有 Agent 判断后跑 `distill-apply` 才落一次原子 commit。

## 开发

```bash
$PY -m pytest tests/ -q     # 74 tests（MCP tool 边界 + 蒸馏 + 生命周期/索引/CLI）
$PY -m mypy src/compound_memory/
```

测试缝：MCP tool 边界（`mcp.Client(server)` 内存直连，无子进程）+ 核心模块单测（scoring / index / store 运维面）。
