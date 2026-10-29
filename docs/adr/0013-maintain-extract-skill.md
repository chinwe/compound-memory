# 记忆维护时把高活性 episode 标为 type=skill：只建议，正文由调用方在 apply 时给出

本文件只记录设计，供后续实施 PR 按文末验收落地。本 PR 不改 `TYPE_SPEC`，不改 `distill_plan` / `dream` / `dream_apply` / `maintain` 的行为，不加 flag，不加 MCP tool。

对齐 ADR-0001：server 零 LLM；判断留在调用方。落库仍走既有 `dream-apply` / `distill-apply`。定时任务与 `maintain` 不静默 apply。

**skill 指什么。** 库内记忆 `type=skill`。它已经在 `model.MEMORY_TYPES` 里，文件落在 `namespaces/<ns>/skill/<id>.md`。Cursor `SKILL.md`、marketplace plugin 里的 skill 文件、以及本仓库 `skills/compound-memory/SKILL.md`，是另一套宿主文件。那些文件不在本决议的读写范围内（phase 2）。

**谁写正文与 key。** server 只在既有只读报告上建议候选。apply 时的 `content`（正文）、`key`、以及选定 `type=skill`，由调用方经 `dream-apply` 或 `distill-apply` 提交。`dream`、`dream --propose`、`maintain` 不生成步骤散文。`distill_apply` 的产物仍是 `origin="distillation"`、证据块显式零起点（ADR-0008），不新造 origin。

## 触发（v1，只用现成门槛）

不新造模糊门。可带 `target_type=skill` 的行，只来自下面这条已经存在的晋升标注。同 key 多版仍是 merge，弱相似仍是 review。

`storage/distill.py` 的 `distill_plan` 先收主候选，再标信号。主候选同时满足：

- `scoring.is_expired` 为假（`valid_until` 已过的跳过），且 `scoring.recency_age` 有值（坏日期跳过）。新近基准是 `last_used` 优先、`created` 兜底。
- 年龄 ≤ `window_days`。函数缺省 `window_days=30`，CLI `distill-plan` / `dream` / `dream-apply` / `maintain` 的 `--window` 原样传入。
- `uses >= min_uses`。缺省 `min_uses=1`。
- `confidence >= min_confidence`。缺省 `min_confidence=0.5`。

高活性 episode 与 `promotion_candidate` 是同一个布尔。主候选进入列表之后：

```text
promotion_candidate = mem.type == "episode" and mem.uses >= PROMOTION_USES_THRESHOLD
```

`PROMOTION_USES_THRESHOLD = 5`（`storage/distill.py`）。缺省 `min_uses=1` 时，`uses >= 5` 已经盖过 uses 那半边活性门。缺省的 `min_confidence=0.5` 与 `window_days=30` 仍各算各的：`uses` 达到 5、但置信度低于当时的 `min_confidence`，或新近年龄大于当时的 `window_days`，都不会成为 `promotion_candidate`。调用方把 `--min-uses` 调到大于 5，或把 `--window` 调短、把 `--min-confidence` 调高，更严的那个生效。

`suggested_action` 的词汇是 `SUGGESTED_ACTIONS = ("merge", "promote", "review")`。`_candidate_suggested_action` 的顺序是：`merge_with` 非空则为 `merge`（压过晋升）；否则 `promotion_candidate` 为真则为 `promote`；否则弱相似（含非对称的 `possible_dup_of`）为 `review`。弱相似门槛是 `DISTILL_DUP_SIM_THRESHOLD = 0.5`（BM25 `normalized_similarity`）。

**只有 `suggested_action` 为 `promote` 的行可以带 `target_type=skill`。** 同一行上 `promotion_candidate` 仍可能为真、但动作已被 `merge_with` 收成 `merge` 的，不带 `target_type`。`review` 行不带。`merge` / `review` 两个动作保持原样。

同 key 多版（已用于 merge，不作为 skill 触发）有两处，下限都是 2，库里没有名为「≥2」的常量（ADR-0011 已写明 `key_duplicates` 的下限是 2）：

- 活性窗内的 `merge_with`：同 ns、同 `type`、同 `key`（`key` 非空）。`dream._merge_id_groups` 与 `_strong_key_signal` 要求组员 `len >= 2`，人数不足则不是合并强信号。
- `key_duplicates`：同一 ns 活动区再扫一遍，跳过 `valid_until` 已过与坏日期，按 `(type, key)` 分组，`len(members) < 2` 则跳过。不受 `window_days` / `min_uses` / `min_confidence` 限制。组上 `suggested_action` 恒为 `merge`，并带 `source_ids`。

`dream`（`storage/dream.py`）的 `distill` 段原样带出 `distill_plan`，不重算这些门槛。`dream_apply` 的入选表仍是 `APPLY_SIGNALS = ("merge_with", "key_duplicates", "promotion_candidate")`。`promotion_candidate` 只有该动作显式 `promote: true` 才入选；`promote` 缺省为 false 时，源 id 即便落在晋升集合里也进 `skipped`，原因是 `REASON_PROMOTION_OPT_IN`（`"promotion_candidate requires explicit opt-in"`）。`promote` 为 true 但源 id 不全在晋升集合里，原因是 `REASON_BELOW_PROMOTION`（`"below promotion threshold"`）。`possible_dup_of` 永远不进 apply 包（`REASON_POSSIBLE_DUP`）。

skill 的落库就用这条 opt-in：调用方提交 `type=skill`、`content`、`key`、`source_ids`、以及 `promote: true`。进程不代写 `content`。

`dream --propose` 的合并包门槛（`PROPOSE_MIN_MEMBERS = 3`、`PROPOSE_MIN_EVIDENCE = 3`、`PROPOSE_MIN_TOKEN_COVERAGE = 0.5`）是 ADR-0011 的同 key 合并包，不是 skill 触发。`dream_proposals` 今天不收 promotion，也不收 `possible_dup_of`。

## 去重

同一 ns 的活动区里已经有一条 `type=skill`、且 `key` 与本条提议将使用的 key 相同，则这条原本会标 `promote` 并带 `target_type=skill` 的候选改为 `suggested_action=review`，不带 `target_type`，也不进入 `suggested_actions` 的 promote 簇。不再静默堆一条新的 skill 提议。

「活动」与 `key_duplicates` 同一人口：该 ns 活动区，跳过 `valid_until` 已过，跳过坏日期。归档区里的 skill 不算。比较用的 key：报告阶段用该 episode 已有的 `key`（episode 没有 key 时，报告阶段还对不上，因为提议 key 还没出现）；apply 阶段用调用方提交的 `key`。

已有的 merge 簇保持 `merge`。那是同 `type` 同 `key`、组员 ≥2 的规则，不改判成 skill，也不因为旁边还有一条 skill 而改名。

`dream-apply` 上，入选信号若是 `promotion_candidate`，且 `type=skill` 的这个 `key` 在该 ns 已有活动 skill，则不调用 `distill_apply`，该动作进 `skipped` 并写明原因。同 `type` 的 skill 多版仍走 `merge_with` / `key_duplicates`，不新开 `APPLY_SIGNALS` 成员。

现状对不上这条去重。`merge_with` 与 `key_duplicates` 的桶是 `(type, key)`，episode 与 skill 不会进同一簇。`promotion_candidate` 不查看是否已有 skill。`review.find_by_key` 只扫 `namespaces/<ns>/<type>/`，冲突域也是同 type。把 `skill.key_conflicts` 改成 true 之后，第二条 skill 会与已有 skill 进 review 队列；它不会看到同 key 的 episode，也挡不住「晋升路径再写一条 skill」。晋升路径的去重是实施 PR 要新写的检查，不是 `key_conflicts` 的副作用。今天 `key_conflicts` 为 false 时，同 key 第二条 skill 直接落盘且不进队列（`tests/test_decision_type.py` 的 `test_types_without_conflict_flag_never_enqueue`）。

## TYPE_SPEC.skill

类型已在 `model.TYPE_SPEC`。当前值与 fact 对照如下。`scoring.TYPE_WEIGHT` 与 `scoring.TAU_DAYS` 由这张表派生。`writing.write_new` 把 `ttl=TTL_DAYS[type]` 写入 frontmatter。

| 字段 | skill 当前 | fact 当前 | 本用例的决定 |
| --- | --- | --- | --- |
| `ttl_days` | `None` | `None` | 保持 `None` |
| `tau_days` | `365.0` | `365.0` | 保持 `365.0`（与 fact 同值） |
| `weight` | `1.0` | `0.9` | 实施 PR 改为 `0.9`，与 fact 同档 |
| `key_conflicts` | `false`（`TypeSpec` 缺省） | `true` | 实施 PR 改为 `true` |

`ttl_days` 与 `tau_days` 已经是意图值，实施 PR 不要改这两个字段。`weight` 当前是 `1.0`，高于 fact 的 `0.9`。`tests/test_scoring.py` 里 `test_type_weight_breaks_full_ties` 的注释写的是 skill(1.0)。改权重是契约变更：只改注释不够时，按契约变更流程动断言。`key_conflicts=true` 会使 `tests/test_model.py` 的 `test_conflict_flag_only_on_key_update_channel_types`（现钉 `{fact, insight, decision}`）和上节的 skill 不入队测试失效，同样留在实施 PR。

普通 `memory_write` 在 `key_conflicts=true` 之后沿用现有冲突语义：新文件仍写出，并追加 review 行（`writing.write_new`）。那是 key 更新通道，与「晋升提议不再堆第二条 skill」是两件事。

## 与 #78 正交

`model.LESSON_ROLES = ("lesson", "avoid")`。`Memory.lesson_role` 缺省 `None`。`write_new` 只在调用方传入时写入。没有路径因为 `type=skill` 填上 `lesson` 或 `avoid`。skill 不默认成教训，也不默认 `avoid`。

## 衰减

skill 不衰减，长寿。现有衰减路径已经跳过它。

`lifecycle.is_decay_candidate` 是 `decay_sweep` 与 `dream` 的 `low_activity_count` 共用的谓词：`mem.ttl is None` 时返回 false。skill 经 `write_new` 写下的 `ttl` 是 `TTL_DAYS["skill"]`，即 `None`，所以 `decay_sweep` 不会归档它。`ARCHIVE_USES_THRESHOLD = 3` 只作用于 `ttl` 非空、且新近年龄大于该 ttl 的记忆。spec story 14 已写 fact / skill / decision 的归档 TTL 为永不。

测试只钉了 fact（`tests/test_lifecycle.py` 的 `test_fact_never_archived`），没有 skill 的同形回归。实施 PR 补上：经正常写入的 skill，`created` 远早于测试时钟的今天，`uses < ARCHIVE_USES_THRESHOLD`，`decay_sweep` 的结果不含它。谓词读的是文件上的 `ttl`。有人把某条 skill 的 frontmatter `ttl` 手改成整数之后，那一条会按手编值进入衰减；豁免钉的是写路径落下的 `ttl=None`。

## maintain 与提议通道

`cli.cmd_maintain` 的顺序保持不变：只读 `dream`（不带 `--propose`，完整报告写入 `<root>/distill/last-plan.json`）→ 可选 `decay_sweep`（仅 `--with-decay`，或环境变量 `RUN_DECAY=1`，`DECAY=1` 同义；默认关）→ `health-snapshot`。不调用 `dream_apply`，不调用 `distill_apply`。任一步失败即停。`scripts/daily-maintain.sh` 只解析 uv 后 exec 这条命令。`scripts/distill-prepare.sh` 只跑不带 `--propose` 的 `dream`。定时任务不 apply。

系统提议沿用已有的 `dream --propose`（ADR-0011，默认关，无 `--auto`）。v1 不给 `maintain` 增加 `--propose`。再叠一个 flag 与「定时路径不带提议」的现约束相抵，本决议不采用。

skill 行不写进 `<root>/distill/dream-proposals.json`。该文件的元素是已经能交给 `dream-apply` 的合并动作，`content` 取簇内原文（ADR-0011）。`_normalize_actions` 要求每条 `content` 为非空字符串，空正文会拒绝整份列表，连同已合格的 merge 行一起失败。进程又不许生成步骤散文，因此不能为了填满字段把 episode 原文抄成 skill 正文。调用方要落 skill 时，另交一份自己写好 `content`、`key`、`type=skill`、`promote=true` 的列表给 `dream-apply`，或直接调用 `distill-apply`。需要合并包的人自己跑 `dream --propose`。`maintain` 与两个定时脚本继续不带该 flag。

## MCP

`server.py` 仍是恰好这 5 个 tool：`memory_write`、`memory_search`、`memory_get`、`memory_link`、`memory_feedback`。本决议不改 `skills/compound-memory/SKILL.md`，不改 `docs/agent-integration.md` 与 `docs/agent-integration.en.md`。实施 PR 若改了 tool 行为或使用规则，再按仓库约定同步那三处与 spec；v1 不新增 tool。

## 验收（实施 PR）

- [ ] `suggested_action` 为 `promote`、且同 ns 没有活动 skill 使用该 episode 的 `key` 时，该候选带 `target_type=skill`。`merge` 行与 `review` 行不带该字段。
- [ ] 同 ns 已有活动 skill 同 key 时，该候选的 `suggested_action` 为 `review`，不带 `target_type`，且不出现在 promote 簇里。已有同 type 的 merge 簇仍是 `merge`。
- [ ] `dream-apply` 的 `promotion_candidate` 路径在该 key 已有活动 skill 时不调用 `distill_apply`，`skipped` 带明确原因。同 type 的 `merge_with` / `key_duplicates` 仍可落库。
- [ ] skill 产物的 `content`、`key`、`type=skill` 来自调用方；`promote: true` 才放行晋升。`dream` / `dream --propose` / `maintain` 不写步骤正文。
- [ ] `maintain` 仍是不带 `--propose` 的 dream → 可选 decay → health-snapshot。无新 flag。`distill-prepare.sh` 与 `daily-maintain.sh` 不 apply。
- [ ] `dream-proposals.json` 不增加缺正文的 skill 行，也不把 episode 原文当作 skill 步骤写入。
- [ ] `TYPE_SPEC["skill"]`：`ttl_days` 仍为 `None`，`tau_days` 仍为 `365.0`，`weight` 改为 `0.9`，`key_conflicts` 改为 `true`。相关契约测试走显式契约变更。
- [ ] 回归：正常写入、陈旧、`uses < 3` 的 skill 不被 `decay_sweep` 归档。
- [ ] 未传入 `lesson_role` 的 skill 写入，该字段仍为空。
- [ ] MCP tool 仍为上面那 5 个。

## 非目标

宿主 `SKILL.md` 与 plugin skill 文件的生成或改写（phase 2）。第六个 MCP tool。`maintain --propose`。进程内 LLM，或由 `dream` / `maintain` 生成步骤散文。定时任务调用 `dream-apply` / `distill-apply`。把 `possible_dup_of` 或同 key merge 簇改判为 skill。用 `PROPOSE_MIN_MEMBERS` 等合并包门槛代替 `PROMOTION_USES_THRESHOLD`。版本号与 PyPI。
