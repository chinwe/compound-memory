# 核对记录

复制本文件再填，一份条件一份。文件名自定，例如 `20261018-pref-concise-tables-B.md`。不要把未替换的本模板当成一次跑分。

`stats` 快照可以用 `uv run python scripts/eval_stats_snapshot.py --root "$COMPOUND_MEMORY_ROOT"` 生成，把生成的 JSON 文件名填到下面。快照是健康度。`uses`、`cross_validated`、`recent_feedback_7d` 只能当旁证，不能单独当作本条通过。

记忆正文可能含项目约定。往 git 里提交填好的记录之前，去掉不该入库的原文；计数类 `stats` JSON 不含正文。

## 运行

| 项 | 值 |
|---|---|
| date | |
| operator | |
| benchmark_id | |
| condition | A（空记忆）或 B（已种入 seed） |
| COMPOUND_MEMORY_ROOT | |
| COMPOUND_MEMORY_AGENT_ID | |
| reader_agent | 跨宿主题才填 |
| stats_snapshot | `docs/eval/results/<时间>-stats.json` 或「未存」 |

## 题目

| 项 | 值 |
|---|---|
| task | 从 `must-hit.yaml` 抄 `prompt` |
| memory_key | A 留空。B 填 `expected_key` |
| feedback_outcome | A 留空。B 填实际调用的 outcome：`success` / `failure` / `contradiction` / `obsolete` / `unknown`。没有调用就写「未反馈」 |
| steps_to_success | 实际命令或 tool 调用顺序。B 应能看到 search，然后 feedback |
| repeat_pitfalls | 这遍有没有重踩该题写明的坑：踩中 / 避开 / 不适用。写一句观察到的原文，不要只写是或否 |
| notes | `explain` 里与本条有关的字段：`confidence`、`uses`、`validated_by`、`evidence.success_count`、`failure_count`、`contradiction_count`。负向题注明 `review-queue` 或 `git-log` 是否对得上 |

## 本遍结论

只选一项，并写依据指回上面哪一格：

- 基线完整（只用于条件 A）
- B 通过（`pass_b` 全部满足，且 `feedback_outcome` 与题目要求一致）
- B 未通过
- 记录不完整

条件 A 的「基线完整」不是有效。全套对外怎么说，见 [`../README.md`](../README.md) 的收口标准。
