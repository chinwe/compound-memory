# 核对记录：fact-serverless-10s 条件 A

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | fact-serverless-10s |
| condition | A（空记忆） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-f10s-a |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存（空库无信息量） |

## 题目

| 项 | 值 |
|---|---|
| task | 给这个平台写一个会依次调用三个下游的 serverless 处理函数，并写明超时预算。不要假设你记得平台文档。 |
| memory_key | （空） |
| feedback_outcome | 未反馈 |
| steps_to_success | `init` → `search "serverless 平台超时预算"`（hits=0，证明空库）→ 直接作答 |
| repeat_pitfalls | 踩中（预期基线）。题面明示「不要假设你记得平台文档」，空库检索又无结果，作答取了无记忆下最常见的平台默认值 30 秒作为超时预算，未写 10 秒 |
| notes | 空库 `search` 返回 0 hits。注：操作者已读过 `must-hit.yaml` 全文，A 遍作答按「无该记忆」视角模拟默认行为，存在阅读污染可能；A 遍只建基线不判有效 |

## 本遍结论

- 基线完整（只用于条件 A）。无记忆时超时预算靠通用默认值猜（30s），与平台真实的 10s 不符——正是 B 遍记忆要防的坑。
