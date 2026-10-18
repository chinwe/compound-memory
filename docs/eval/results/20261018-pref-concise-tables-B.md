# 核对记录：pref-concise-tables 条件 B

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | pref-concise-tables |
| condition | B（已种入 seed） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-pref-b |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | docs/eval/results/20261018T095014Z-stats.json |

## 题目

| 项 | 值 |
|---|---|
| task | 用一段说明 compound-memory 的五个 MCP tool 各做什么。 |
| memory_key | user-reply-style |
| feedback_outcome | success |
| steps_to_success | `init` → `write`（seed，id=20261018_c6480a）→ `search "回复格式 表格 偏好"`（1 hit，similarity 1.0）→ 采纳并作答 → `feedback 20261018_c6480a agent-claude --outcome success` → `explain 20261018_c6480a` |
| repeat_pitfalls | 避开。作答为五行短表：`memory_search` 检索 / `memory_write` 写入 / `memory_get` 按 id 读 / `memory_link` 建双向链接 / `memory_feedback` 回填结果——未出现长段落 |
| notes | `explain`：confidence 0.6、uses 1、evidence.success_count 1、validated_by [agent-claude]。B 的作答确实先经 `search`，非碰巧写对 |

## 本遍结论

- B 通过。`pass_b` 全部满足：search 命中 user-reply-style、回复是短表、feedback outcome=success、success_count ≥ 1。
