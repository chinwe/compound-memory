# API 契约：设计意图文档

> **本文档不承载强制力。** 契约的唯一强制载体是 `tests/contracts/`
> characterization 测试套件（经既有 CI pytest 门禁生效）；本文档只记录
> 设计意图与五要素全文，**允许漂移，冲突时以测试为准**——它不是需要
> 同步维护的第二真相源。决议出处：#25（载体与组织）、#26（语义缺口
> 裁决 P1–P6 与 D1–D4）、#31（审计载体与消息模板）；取舍另见
> [ADR 0002](../adr/0002-api-contract-carriers.md) 与
> [ADR 0006](../adr/0006-audit-carrier.md)。

## 三载体分工

| 载体 | 钉什么 | 强制机制 |
| --- | --- | --- |
| 类型签名（mypy） | 结构形状（签名、参数类型） | 既有 CI 门禁 |
| `tests/contracts/`（按动词组织） | 行为语义，**唯一带强制力** | 既有 CI pytest 门禁 |
| 本文档 | 设计意图与背景取舍 | 无（漂移即失效信号） |

## 五要素模板

每个动词的契约按五要素刻画（`tests/contracts/` 中每个文件即一个动词的落地）：

1. **输入**：按等价类描述与测试（合法形状、非法 ns、坏 key、越权 reader……），不穷举非法值；
2. **权限**：ns 门禁矩阵——`agent-<name>` 私有 ns 仅属主（全称 `agent-x` / 短名 `x` 皆可），
   `_shared` 无需身份；矩阵即测试表；
3. **side effects**：锚定可观测点——git 提交发生与否（消息模板见下表）、文件落点
   （活动区 `namespaces/<ns>/<type>/` / 归档区 `archive/` / 队列 artifact）、索引同步；
4. **错误行为**：类型级——调用方错误抛 `ValueError` / `PermissionError`
   （CLI 翻译为 stderr JSON + exit 2，MCP 翻译为 `is_error`）；目标不存在返回
   `{"found": False}`（按 id 动词恒含 `found` 键，非法 mem_id 等价于不存在）；
5. **一致性保证**：只收编既有不变量——活动记忆必被索引、归档必不在索引
   （revive/feedback 复活时同步回索引）、写动词读-改-写全程持写锁
   （原子拒绝不留半状态）、batch 写穿（批尾一次 flush + 一次 commit，
   重开 store 后批写条目可检索）。不发明新保证。

## git 提交消息模板表（#31 决议，side effects 锚点）

「git 历史即审计史」：写路径提交消息格式钉死如下，`tests/contracts/anchors.py`
的 `COMMIT_TEMPLATES` 与本表同源。消息只含 id / ns / type / agent 名，**不含正文**
（隐私约束）。读路径（search / get / distill_plan）不产生提交。

| 动词 | 模板 |
| --- | --- |
| write | `write {id} ({type}/{ns}) by {source}` |
| feedback | `feedback {id} by {agent}: uses={uses} conf={confidence}`（前瞻：#43 evidence-based confidence 实施时将扩展 outcome 段，届时按「契约变更」流程同步本表与 `COMMIT_TEMPLATES`） |
| forget | `forget {id} by {agent}`（携带理由时追加 `: reason={reason}`；reason 是动机短语、单行限 80 字符、不含记忆正文——ADR-0009/#48 契约变更） |
| link | `link {a} <-> {b}` |
| decay（decay_sweep 归档） | `decay: archive {ids}` |
| revive | `revive {id}` |
| distill_apply | `distill apply {id} <- {source_ids}`（逗号+空格分隔，去重保序） |
| review_resolve | `review resolve {n} entries (archived: {ids})`（无归档时省略括号段） |
| batch（默认消息） | `batch write {n} entries`（失败收尾加 ` (partial)` 后缀） |
| 特殊：启动孤儿对账 | `orphan changes recovered` |
| 特殊：首建 | `init compound-memory store` |

## 按动词的语义意图（store 层，Tier 1 全五要素）

- **write**：落库正门。id 形如 `YYYYMMDD_hex6`；缺省 confidence 0.5、ns `_shared`、
  ttl 取自类型规格（episode 90 / fact、skill 与 decision 永不 / insight 180）。key 校验
  （小写字母数字段+短横线）与 validity 校验（ISO 日期、from ≤ until）在 write 单点（P6）。
  冲突判定（P3，#46 起类型维表驱动）：同 ns ∧ 同 type（`TypeSpec.key_conflicts` 标记类型：
  fact/insight/decision）∧ 同 key ∧ `content.strip()` 不等
  ⇒ 入 review 队列并在返回值标 `conflict: true` + `conflicts_with`；episode/skill append-only 不判。
- **search**：检索 = 候选（词面 ∪ 向量 KNN，RRF 融合）+ 排序（`scoring.rank` 单点）。
  空白 query 返回空列表（合法）；非法 ns 抛 ValueError（静默空结果是错误契约）；
  缺省 top_k=5；ns 缺省为双通道（`_shared` ∪ 调用方自有私有 ns，身份已知时），
  显式 ns 是单 ns 精确语义。hit 形状 9 键 + 默认内嵌至多 3 个邻居。过期
  （valid_until 已过）与归档记忆不可见。**不产生提交**。
- **get**：按 id 恒读——归档、过期（D3：valid_until 只管检索可见性）均可读。
  links 输出与邻居对跨 ns 遗留链脱敏。**不产生提交**。
- **feedback**：复利闭环。公式（P1）：confidence 每次 +0.1，另 +0.15 仅当
  「新验证者 ∧ ≠ source」；`validated_by` 去重（同 agent 重复反馈不再加验证分）；
  round 3 位；封顶 1.0。side effects（P2）：uses+1、last_used=today、**归档记忆自动
  复活**（回活动区 + 索引同步）、单次 commit（每次 feedback 一 commit，#31 裁决）。
- **link**：双向关联。跨 ns 禁止（ValueError，原子）；自链 ValueError；
  私有 ns 仅属主（D1：可选 `agent` 参数，对称 feedback——link 是最后一个
  无身份写入口，已收口）；缺失 id 返回 found 信封（先于门禁）。
- **revive**：归档复活的写侧出口。私有 ns 仅属主（与 get 同属按 id 读路径）；
  活动记忆上的 revive 是幂等零操作（不产生提交）。
- **forget**：终态遗忘（ADR-0009/#48）。文件经 remover 缝物理移出（作用域 =
  活动区 ∪ 归档区）+ 恰好一条 forget 提交，内容仅存 git 历史；私有 ns 仅属主
  （role=agent，与 feedback 同规）；幂等——不存在/已遗忘返回 `{"found": False}`
  零提交，命中返回删除前快照；顺带幂等清该 id 的 review 队列行（无行是常态，
  区别于 review_resolve 按 ids 的未命中 ValueError，故 ReviewQueue 另设
  `clear_for` 而不走 `resolve`）；links 悬空容忍不摘链（find→None 容错覆盖
  邻居召回与蒸馏候选）；无复活通道（feedback/revive 对被遗忘记忆返回
  found: False）；stats 不设 forgotten 计数（三态模型零新增例外）；入口仅
  store + CLI（MCP 恰好 5 tool 红线不动）。
- **distill_plan**：确定性候选扫描（判断归调用方）。归档区与过期记忆不参与；
  活性门（uses/confidence）+ 窗口（新近基准 last_used 优先）；产出主候选
  （merge_with / possible_dup_of / promotion_candidate 三类信号）与
  key_duplicates 专项段。扫私有 ns 须属主（候选带正文）。**不产生提交**。
- **distill_apply**：蒸馏落库（原子）。失败语义（P5）：missing 源 ⇒
  `{"found": False, "missing": [...]}` 零操作零提交；跨 ns 源 ⇒ ValueError 整体拒绝；
  源去重保序；已归档源跳过搬运但仍计入清单。成功：产物（origin=distillation、
  links 溯源全部源）+ 源批量归档收进恰好一次 commit（消息含产物 id 与源清单）。
- **review_resolve**：冲突裁决登记。输入互斥（ids 或 --all）；未命中 id ⇒
  ValueError 原子拒绝（P4）。传入 id = 裁决废置方：清行同时归档它，对侧保留；
  `--all` 只清行、不归档、不产出 rows。D2：私有 ns 的行仅属主可 resolve
  （可选 reader；`--all` 对不可见行静默保留并如实计数 remaining）；**展示维持全量**
  （张力：CLI 是本机信任边界、MCP 5 tool 不暴露队列、行含 content[:40] 片段——
  这是有意决策而非遗漏）。
- **batch**：批量落库正门。逐条校验写穿（批内非法条目照样抛错，已写入条目以
  partial 提交后原样上抛——「落地即已提交」）；批尾一次索引 flush + 一次 commit；
  嵌套 batch 是调用方错误；所有写动词（feedback/link/decay/…）的提交在批内
  一并延迟（单点拦截）。

## 覆盖分层（非三层对称）

- **store 层**（上节）：全五要素 characterization；
- **MCP 层**：只钉 5 个 tool 名、返回形状（`{"hits": ..., "count": n}` 包装只在
  MCP 层）、`structured_output=False` 单份序列化、异常 → `is_error` 翻译
  （`tests/contracts/test_mcp_surface.py`）；语义不重测；
- **CLI 层**：只钉 17 个子命令名与关键 flags（含 D1/D2 的 `link --agent`、
  `review-resolve --reader`、ADR-0009 的 `forget --agent/--reason`）
  （`tests/contracts/test_cli_surface.py`，
  argparse 结构断言）；行为不重测（CLI 是薄 adapter；调用方错误统一翻译为
  stderr JSON + exit 2）；
- **Tier 2 薄钉**（形状 + 错误）：`find` / `decay_sweep` / `review_queue` /
  `rebuild_index` / `stats` / `git_log` / `lexical_candidates`
  （`tests/contracts/test_ops_tier2.py`）。

## 可选能力声明（vec）

向量召回属可选能力（`vec` extra + 本地模型）：**契约测试以显式注入 embedder
seam（确定性词袋编码）声明该能力**（`tests/contracts/test_search.py::
TestVectorCapability`），不用 skip——未装 vec 的环境里「降级纯词面」本身就是
被钉的缺省行为，不是被跳过的用例。

## 契约变更纪律（拆分迁移期）

- 每个拆分切片 PR 必须保持 `tests/contracts/` 全绿（契约跟测试走）；
- 契约测试默认**冻结**：发现刻画不准需要修改时，PR/commit 必须显式标注
  「契约变更」并说明原因——防拆分顺手改行为、测试跟着放水；
- 提交消息模板表的改动（消息措辞/字段顺序）同样是契约变更
  （`tests/contracts/anchors.py::COMMIT_TEMPLATES` 与本表同源联动）。
