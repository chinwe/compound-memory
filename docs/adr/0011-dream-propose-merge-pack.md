# dream --propose：显式写出可 dream-apply 的候选合并包，默认关闭

（#97，母议题 #96 只取 P0-2 这一刀。）`dream` 默认仍是 ADR-0001 的只读报告。加上 **`--propose`** 时，另外写出一份已经是 `dream-apply` 输入形状的合并提议；人读过、可改、再跑 `dream-apply`。系统提议，人工批准。没有 `--auto`（那个名字像自动落库）。没有 `--apply`。不调用 `dream_apply`，不新增 MCP tool，定时脚本 `scripts/distill-prepare.sh` 继续只跑不带该 flag 的 `dream`。CLI `maintain`（及薄包装 `scripts/daily-maintain.sh`）同样不带 `--propose`，也不调用 dream-apply。

**唯一输出。** 包只写 `<root>/distill/dream-proposals.json`（与 `last-plan.json` 同级，`distill/` 已在库 `.gitignore`，这次写出不产生 commit）。文件内容就是 JSON 列表，没有外套对象。stdout 仍是原来的只读报告，形状不变，列表不进报告。不设第二条通道（不打到 stdout，也不另给一个「报告里的 proposals 字段」）。`--propose` 每次都覆盖该文件。不加 flag 时不创建、不覆盖、不删除它。

**空。** 没有簇过门槛时仍打印只读报告，并把该文件写成 `[]`。stderr 在原摘要后再打一行 `dream proposals ns=<ns>: count=0 empty path=<path>`。空列表交给 `dream-apply` 会按既有规则拒绝（非空列表才合法）——这是「这次没有提议」，不是失败的 dream。覆盖成 `[]` 是为了清掉上一次留下的包，避免把旧提议当成这次的结果。

**门槛**（常量在 `storage/dream.py`：`PROPOSE_MIN_MEMBERS` / `PROPOSE_MIN_EVIDENCE` / `PROPOSE_MIN_TOKEN_COVERAGE`）。人口与 `key_duplicates` 相同：该 ns 活动区，跳过 `valid_until` 已过，跳过坏日期。不吃 `dream` 的 `--window` / `--min-uses` / `--min-confidence`。然后四道关，全过才出一条动作；任一道不过，这个 key **整簇不进包**（不拆子集）：

1. 同 `type` 同 `key`，且有 key。人数 **N = 3**。`key_duplicates` 的下限是 2，库里没有「≥3」的现成常量，所以 N 新定在这里。
2. 词面覆盖率。对正文（**不含 key**：key 已经是分簇条件，再算进去会让很短的不同正文靠共享 key 过线）取 `scoring.tokenize` 的 token 集合（小写拉丁整词 + CJK bigram）。有序对的覆盖率

    `coverage(a → b) = 0`（`tokens(a)` 为空）否则 `|tokens(a) ∩ tokens(b)| / |tokens(a)|`

    簇内每一对 `i ≠ j` 都要 `coverage(i → j) ≥ 0.5`（`PROPOSE_MIN_TOKEN_COVERAGE`）。两个方向都要过。不用封闭簇上的 BM25 / `normalized_similarity`：`DISTILL_DUP_SIM_THRESHOLD`（0.5）是在 `distill_plan` 的混合候选池上标定的；池子只剩簇自己时，共享 token 的 df 等于文档数，IDF 塌掉，仓库里那对近重复（「…交给 agent 判断」/「…交给调用方判断」）互相只有约 0.16，过不了 0.5。把矩阵放大到整个 ns 又会让无关记忆决定这个簇过不过。`extraction` 标注复述时已经因为同样的结构性偏低改用 token 覆盖率 0.5；这里沿用那种度量，常量放在提议门槛旁边，不从 extraction 引（避免 storage → extraction 的环）。
3. 证据总量 **M = 3**（与 N 同值，不是 `PROMOTION_USES_THRESHOLD`——那是 episode 晋升）。簇内求和

    `evidence_total = success_count + failure_count + contradiction_count`

    取 `evidence_view`：无证据块的旧记忆 `success_count = uses`，另两项为 0，总量等于 uses。三次计数都算证据事件，不把 uses 再加一遍。
4. **未裁决 contradiction 不进包。** 排除集只来自 `ReviewQueue.pending_contradictions()`（feedback `outcome=contradiction` 的队列行）。先剔除再数人数、证据和覆盖率；剔除后不足 N 则该 key 没有动作，被剔除的 id 不会出现在任何 `source_ids` 里。同 key 内容冲突行、`contradiction_pairs` 报告段都不在这道门里——冲突行正是同 key 多版还在活动区的常态，不是冻结。

**Apply 契约。** 列表元素只有 `content`、`type`、`source_ids`、`key`。这是 `dream-apply` 已接受的字段子集（另有可选 `confidence` / `promote`，这里不写）。不发明第二种格式。`content` 不是摘要：无 LLM，取簇内证据总量最高的那条原文；并列再比 `confidence`、较新的 `created`、较小的 `id`。`source_ids` 按 `created`、`id` 升序，是过门槛的全体成员。`confidence` 不抄源（ADR-0008：蒸馏产物不继承证据；省略则 `dream-apply` 照旧把置信度交给 `distill_apply` 的缺省）。`promote` 省略，即不晋升。人可以在批准前改文件里的正文，改完仍须是同一形状。`dream-apply` 再用现有的 `merge_with` / `key_duplicates` 门槛筛一遍：这份列表在写出时是当时库的子集，所以当时能过；库若在提议和批准之间变了，跳过的进 `skipped`，不会绕过门槛落库。

**非目标。** 默认自动落库；无人批准的合并；`--auto`；把 promotion 或 `possible_dup_of` 放进包；置信度去通胀；JSONL/WAL；Git 提交合并；向量增量索引；project 硬隔离；health-trend；新 MCP tool；版本号 / PyPI。
