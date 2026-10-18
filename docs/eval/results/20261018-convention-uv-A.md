# 核对记录：convention-uv 条件 A

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | convention-uv |
| condition | A（空记忆） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-cuv-a |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存（空库无信息量） |

## 题目

| 项 | 值 |
|---|---|
| task | 给新同事写三条在本仓库跑测试的命令。 |
| memory_key | （空） |
| feedback_outcome | 未反馈 |
| steps_to_success | `init` → `search "仓库 跑测试 命令 依赖管理"`（hits=0，证明空库）→ 直接作答 |
| repeat_pitfalls | 踩中（预期基线）。空库无约定记忆，按通用 Python 项目习惯作答：`pip install -e ".[dev]"`、`pip install pytest`、`pytest tests/ -q`——出现 pip，三条里没有 uv run |
| notes | 空库 `search` 返回 0 hits。注：操作者已读过 `must-hit.yaml` 与仓库 AGENTS.md，A 遍按「无该记忆」视角模拟通用写法；A 遍只建基线不判有效 |

## 本遍结论

- 基线完整（只用于条件 A）。无记忆时命令默认走 pip + 裸 pytest，与仓库 uv 约定不符。
