# 核对记录：convention-no-tmp-path 条件 B

## 运行

| 项 | 值 |
|---|---|
| date | 2026-10-18 |
| operator | agent-zcode（ZCode 宿主内执行） |
| benchmark_id | convention-no-tmp-path |
| condition | B（已种入 seed） |
| COMPOUND_MEMORY_ROOT | $TMPDIR/compound-memory-eval-ntp-b |
| COMPOUND_MEMORY_AGENT_ID | agent-claude |
| reader_agent | — |
| stats_snapshot | 未存 |

## 题目

| 项 | 值 |
|---|---|
| task | 给本仓库加一个会写临时目录的 pytest，并给出夹具写法。只写测试代码草稿。 |
| memory_key | pytest-fixture |
| feedback_outcome | success |
| steps_to_success | `init` → `write`（decision seed，id=20261018_2c043f）→ `search "pytest tmp_path fixture 临时目录"`（1 hit）→ 采纳并作答 → `feedback 20261018_2c043f agent-claude --outcome success` → `explain` |
| repeat_pitfalls | 避开。草稿不使用 tmp_path，改指向仓库现成夹具：`def test_writes_log(tmp_store):  # conftest 现成 fixture，落到 .test-tmp/`，并附一行注释说明为什么不用 tmp_path |
| notes | `explain`：confidence 0.6、uses 1、evidence.success_count 1、validated_by [agent-claude]。夹具选择来自命中的 pytest-fixture 记忆 |

## 本遍结论

- B 通过。`pass_b` 全部满足：search 命中 pytest-fixture、草稿不用 tmp_path 并指向仓库现成 fixture、feedback outcome=success。
