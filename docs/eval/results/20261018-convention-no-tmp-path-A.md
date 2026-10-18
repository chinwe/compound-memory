# 核对记录：convention-no-tmp-path 条件 A

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | convention-no-tmp-path |
| condition | A（空记忆） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-ntp-a |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存（空库无信息量） |

## 题目

| 项 | 值 |
|---|---|
| task | 给本仓库加一个会写临时目录的 pytest，并给出夹具写法。只写测试代码草稿。 |
| memory_key | （空） |
| feedback_outcome | 未反馈 |
| steps_to_success | `init` → `search "pytest tmp_path fixture 临时目录"`（hits=0，证明空库）→ 直接作答 |
| repeat_pitfalls | 踩中（预期基线）。草稿用了 pytest 通用惯用法：`def test_writes_log(tmp_path): p = tmp_path / "log.txt"; ...`——把内置 tmp_path 当夹具，不知本仓库已禁用 |
| notes | 空库 `search` 返回 0 hits。注：操作者已读过仓库 AGENTS.md（其中恰有这条真实约定），A 遍按「无该记忆」视角模拟通用写法；A 遍只建基线不判有效 |

## 本遍结论

- 基线完整（只用于条件 A）。无记忆时草稿默认用 tmp_path，与仓库约定冲突。
