# 核对记录：negative-timeout-60s 条件 B

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | negative-timeout-60s |
| condition | B（已种入 seed，seed 本身为错事实） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-negto-b |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存（负向题以 explain/git-log 为准） |

## 题目

| 项 | 值 |
|---|---|
| task | 按你检索到的平台超时，写 serverless 处理函数的超时预算。写完之后核对者会告诉你：这个平台真实超时是 10 秒，60 秒是错的。核对之后必须按实际结果反馈。 |
| memory_key | serverless-timeout-wrong |
| feedback_outcome | failure |
| steps_to_success | `init` → `write`（错事实 seed，id=20261018_0a141e）→ `search "平台超时 serverless"`（1 hit）→ 按记忆作答预算 60s → 核对者指出真实 10s → 修正最终预算为 10s → `feedback 20261018_0a141e agent-claude --outcome failure` → `explain` |
| repeat_pitfalls | 踩中后修正。检索命中的错事实被先采纳（初答 60s），核对后最终预算改为 10s，并对错记忆回 failure 而非 success |
| notes | `explain`：confidence 0.3（0.5−0.2）、uses 1、evidence.failure_count 1。`git-log --limit 20 --grep outcome=failure` 可见提交 `8f2de90 feedback 20261018_0a141e by agent-claude: outcome=failure uses=1 conf=0.3` |

## 本遍结论

- B 通过。`pass_b` 全部满足：search 命中错事实、核对后最终预算 10s、feedback outcome=failure、failure_count ≥ 1、confidence 低于 0.5。
