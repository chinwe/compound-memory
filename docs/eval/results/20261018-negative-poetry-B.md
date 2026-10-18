# 核对记录：negative-poetry 条件 B

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | negative-poetry |
| condition | B（已种入 seed，seed 本身为错决策） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-negpo-b |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存（负向题以 explain/review-queue/git-log 为准） |

## 题目

| 项 | 值 |
|---|---|
| task | 你要给本仓库加一个开发依赖。先检索记忆再决定命令。核对者随后确认：仓库由 uv 管理，没有 poetry.lock。请按这个事实处理你检索到的记忆。 |
| memory_key | toolchain-poetry-wrong |
| feedback_outcome | contradiction |
| steps_to_success | `init` → `write`（错决策 seed，id=20261018_d87d57）→ `search "本仓库 新增依赖 命令"`（1 hit）→ 记忆建议 `poetry add` → 核对者确认仓库由 uv 管理、无 poetry.lock → 驳回该记忆 → `feedback 20261018_d87d57 agent-claude --outcome contradiction` → `explain` → `review-queue` → `git-log --limit 20 --grep outcome=contradiction` |
| repeat_pitfalls | 避开（未把错建议写进最终方案）。最终命令改为 `uv add --dev <pkg>`，并对错记忆回 contradiction 而非 success |
| notes | `explain`：confidence 0.5（contradiction 冻结，不升不降）、uses 1、evidence.contradiction_count 1。`review-queue` 含一行：`2026-10-18 contradiction 20261018_d87d57: by agent-claude (本仓库的依赖管理是 poetry，新增依赖用 poetry add。)`。`git-log` 可见提交 `2a20879 feedback 20261018_d87d57 by agent-claude: outcome=contradiction uses=1 conf=0.5` |

## 本遍结论

- B 通过。`pass_b` 全部满足：search 命中错决策、核对后回 contradiction、contradiction_count ≥ 1、confidence 保持 0.5 冻结、review-queue 与 git-log 均有该 id。
