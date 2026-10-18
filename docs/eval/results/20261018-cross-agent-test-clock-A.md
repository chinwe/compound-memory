# 核对记录：cross-agent-test-clock 条件 A

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | cross-agent-test-clock |
| condition | A（空记忆） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-cross-a |
| COMPOUND_MEMORY_AGENT_ID | agent-workbuddy |
| reader_agent | agent-workbuddy（本遍即读取方身份） |
| stats_snapshot | 未存（空库无信息量） |

## 题目

| 项 | 值 |
|---|---|
| task | 写一条会断言日期的测试草稿，日期从哪来写清楚。 |
| memory_key | （空） |
| feedback_outcome | 未反馈 |
| steps_to_success | `init` → `search "CLOCK_DATE"`（hits=0，证明空库）→ 直接作答 |
| repeat_pitfalls | 踩中（预期基线）。草稿日期取自真实墙钟：`assert ledger.row(date=...)["day"] == date.today().isoformat()` 式写法，未指向任何 conftest 固定时钟——与 pass_a 预期一致 |
| notes | 空库 `search` 返回 0 hits。本遍只建立基线，不判有效 |

## 本遍结论

- 基线完整（只用于条件 A）。草稿贴真实墙钟，与 `pass_a` 的预期观察一致。
