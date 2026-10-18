# 核对记录：negative-timeout-60s 条件 A

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | negative-timeout-60s |
| condition | A（空记忆） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-negto-a |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存（空库无信息量） |

## 题目

| 项 | 值 |
|---|---|
| task | 按你检索到的平台超时，写 serverless 处理函数的超时预算。写完之后核对者会告诉你：这个平台真实超时是 10 秒，60 秒是错的。核对之后必须按实际结果反馈。 |
| memory_key | （空） |
| feedback_outcome | 未反馈 |
| steps_to_success | `init` → `search "平台超时 serverless"`（hits=0，证明空库）→ 直接作答 |
| repeat_pitfalls | 不适用（pass_a 不要求踩坑，只要求基线完整）。空库检索无结果，题面在核对前未给秒数；作答自拟预算 30s（AWS Lambda 类平台的常见默认），未追问 |
| notes | 空库 `search` 返回 0 hits。A 遍不要求 feedback，基线完整即可 |

## 本遍结论

- 基线完整（只用于条件 A）。无记忆时模型自行假设超时秒数（30s），与真实 10s 不符——正是 B 遍记忆要防的坑。
