# 核对记录：cross-agent-test-clock 条件 B

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | cross-agent-test-clock |
| condition | B（已种入 seed） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-cross-b |
| COMPOUND_MEMORY_AGENT_ID | agent-claude（写入方）；agent-workbuddy（读取与反馈方）；agent-zcode（协议外补充的第二验证宿主） |
| reader_agent | agent-workbuddy |
| stats_snapshot | docs/eval/results/20261018T095244Z-stats.json |

## 题目

| 项 | 值 |
|---|---|
| task | 写一条会断言日期的测试草稿，日期从哪来写清楚。 |
| memory_key | test-clock |
| feedback_outcome | success |
| steps_to_success | agent-claude：`init` → `write`（seed，id=20261018_21bf11）→ agent-workbuddy：`search "CLOCK_DATE"`（1 hit）→ 采纳并作答 → `feedback 20261018_21bf11 agent-workbuddy --outcome success` → `explain`；补充：agent-zcode 对同一条再回一次 `success` |
| repeat_pitfalls | 避开。草稿日期来源写明 conftest 的 `CLOCK_DATE` 固定时钟：`assertfmt(day) == CLOCK_DATE`，未贴真实墙钟 |
| notes | 首次 `explain`（仅 workbuddy 验证后）：validated_by=[agent-workbuddy]、success_count 1、confidence 0.75（0.5+0.1 同源+0.15 跨宿主）。agent-zcode 补验后：validated_by=[agent-workbuddy, agent-zcode]、confidence 1.0、`stats.cross_validated` 0→1。注：单验证宿主时 cross_validated=0 不算「没读到」，与文档说明一致；补验一步为协议外演示，展示计数抬升路径 |

## 本遍结论

- B 通过。跨宿主种入/读取/反馈成立：agent-workbuddy 的 `search` 命中 agent-claude 种入的 test-clock，草稿改用 CLOCK_DATE，feedback success，`validated_by` 含 agent-workbuddy。
