# 核对记录：pitfall-search-top-k 条件 B

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | pitfall-search-top-k |
| condition | B（已种入 seed） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-tk-b |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存 |

## 题目

| 项 | 值 |
|---|---|
| task | 写一段核对步骤：刚写入的 8 条记忆是否都能被 search 看见。 |
| memory_key | search-top-k |
| feedback_outcome | success |
| steps_to_success | `init` → `write`（insight seed，id=20261018_34e6dd）→ `search "search 可见性 核对 top_k"`（1 hit）→ 采纳并作答 → `feedback 20261018_34e6dd agent-claude --outcome success` → `explain` |
| repeat_pitfalls | 避开。步骤明确写出：search 必须显式 `top_k>=8`（实际建议再放大留余量），或改为断言候选集合；并注明「只看默认五条会把截断误当成丢更新」 |
| notes | `explain`：confidence 0.6、uses 1、evidence.success_count 1、validated_by [agent-claude]。步骤里的 top_k 处理直接来自命中的 search-top-k 记忆 |

## 本遍结论

- B 通过。`pass_b` 全部满足：search 命中 search-top-k、步骤写出 top_k 至少 8（含断言候选集合的替代）、feedback outcome=success。
