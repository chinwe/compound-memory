# 核对记录：negative-poetry 条件 A

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | negative-poetry |
| condition | A（空记忆） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-negpo-a |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存（空库无信息量） |

## 题目

| 项 | 值 |
|---|---|
| task | 你要给本仓库加一个开发依赖。先检索记忆再决定命令。核对者随后确认：仓库由 uv 管理，没有 poetry.lock。请按这个事实处理你检索到的记忆。 |
| memory_key | （空） |
| feedback_outcome | 未反馈 |
| steps_to_success | `init` → `search "本仓库 新增依赖 命令"`（hits=0，证明空库）→ 直接作答 |
| repeat_pitfalls | 不适用（pass_a 只要求基线完整）。空库无错误记忆可反驳；作答按仓库现状给出 `uv add --dev <pkg>` |
| notes | 空库 `search` 返回 0 hits。无 feedback 可回（没有检索到任何记忆） |

## 本遍结论

- 基线完整（只用于条件 A）。无记忆可反驳，作答本身正确但未被记忆加持。
