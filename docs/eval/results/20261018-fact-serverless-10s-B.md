# 核对记录：fact-serverless-10s 条件 B

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | fact-serverless-10s |
| condition | B（已种入 seed） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-f10s-b |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | docs/eval/results/20261018T101110Z-stats.json |

## 题目

| 项 | 值 |
|---|---|
| task | 给这个平台写一个会依次调用三个下游的 serverless 处理函数，并写明超时预算。不要假设你记得平台文档。 |
| memory_key | serverless-timeout |
| feedback_outcome | success |
| steps_to_success | `init` → `write`（seed，id=20261018_011846）→ `search "serverless 平台超时预算"`（1 hit）→ 采纳并作答 → `feedback 20261018_011846 agent-claude --outcome success` → `explain` |
| repeat_pitfalls | 避开。作答明确写「整个处理函数（含三次下游调用）必须停在 10 秒平台超时内」，三次下游给出 3s/4s/2s 加 1s 余量的分解——预算来自命中的记忆而非猜测 |
| notes | `explain`：confidence 0.6、uses 1、evidence.success_count 1、validated_by [agent-claude]。作答前确有 `search` 命中 serverless-timeout，非碰巧写对 |

## 本遍结论

- B 通过。`pass_b` 全部满足：search 命中 serverless-timeout、预算写 10 秒、feedback outcome=success。
