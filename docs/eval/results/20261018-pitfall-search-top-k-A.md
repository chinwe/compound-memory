# 核对记录：pitfall-search-top-k 条件 A

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | pitfall-search-top-k |
| condition | A（空记忆） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-tk-a |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存（空库无信息量） |

## 题目

| 项 | 值 |
|---|---|
| task | 写一段核对步骤：刚写入的 8 条记忆是否都能被 search 看见。 |
| memory_key | （空） |
| feedback_outcome | 未反馈 |
| steps_to_success | `init` → `search "search 可见性 核对 top_k"`（hits=0，证明空库）→ 直接作答 |
| repeat_pitfalls | 踩中（预期基线）。步骤写的是「用同一关键词 search，确认返回结果包含全部 8 条」——没有提 top_k，实际跑起来默认 top_k=5，最多只见 5 条：截断会被误判成丢更新 |
| notes | 空库 `search` 返回 0 hits。注：操作者已读过 `must-hit.yaml`，A 遍按「无该记忆」视角模拟朴素写法；A 遍只建基线不判有效 |

## 本遍结论

- 基线完整（只用于条件 A）。朴素核对步骤隐含依赖默认 top_k=5，对 8 条的目标必然截断。
