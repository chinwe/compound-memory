---
name: compound-memory
description: 本机跨 Agent 共享记忆库 compound-memory 的使用规范：接到非琐碎任务先 memory_search 查记忆；采纳记忆后 memory_feedback 强化；任务结束沉淀稳定事实（type/key/source 约定）；跑记忆库运维或蒸馏（doctor/stats/decay/revive/review-queue/distill）；检索、写入或注入异常时排查。
---

# compound-memory 使用规范

本机跨 Agent 共享记忆库：MCP 服务 `compound-memory`（5 个工具是唯一读写边界），存储 `~/.agents/memory`，CLI 做运维与蒸馏。接入配置（各宿主 MCP 注册）见仓库 `docs/agent-integration.md`；英文精简版（uvx、必配 agent id、检索→反馈→写入）见 `docs/agent-integration.en.md`。宿主 env **必须**设稳定的 `COMPOUND_MEMORY_AGENT_ID`（与下面的 `source` 相同）。未设置或空白是共享 `_shared` 的硬失败：`stats` 与 `doctor` 退出码 1，诊断仍打印；MCP 工具调用不因此中断，server 启动日志会标明。

## 三个必做动作（复利闭环）

1. **任务开始先检索**：接到非琐碎任务，先 `memory_search` 按任务关键词查相关记忆（用户偏好、项目背景、环境坑）。缺省 `top_k=5`，要看覆盖面就显式放大。默认检索即双通道：`_shared` + 本宿主私有 ns（身份已知时自动并入，私有条目无需单独补搜）；显式传 `ns` 则只搜该 ns（精确语义）。**project 作用域（ADR 0010）**：检索缺省 fail-closed——不传 `project` 只见全局（未标注）记忆；宿主使用规则声明了工作区项目时，检索与写入都带上该 `project=<slug>`，即可见 全局 ∪ 该项目。
2. **采纳即反馈**：命中且**实际采纳**后必须调 `memory_feedback`（`agent` 填本宿主 source id）——复利闭环的核心动作，漏掉它记忆库就不增值。归档记忆被 feedback 自动复活。反馈带 outcome（缺省 `success`）：记忆**用对了**保持缺省；**误导了你**报 `failure`（置信度 −0.2，可降到地板 0.05，重复累计）；**内容有争议**报 `contradiction`（数值冻结、登记冲突队列待裁决）；**已被取代**报 `obsolete`（立即归档）；说不清就报 `unknown`（uses 与 last_used 仍推进，置信度不动）。每个 outcome 都推进 uses 与 last_used。置信度是证据正确性、可升可降：success 与跨宿主首验升它，failure 降它；同宿主的 success 增益累计封顶 0.3——单宿主天花板 0.8，以上只能靠跨宿主验证（ADR 0012；到顶后 feedback 照记账，数值不再升）。
3. **任务结束沉淀**：会话确认的稳定事实（用户偏好、项目约定、环境限制、踩坑结论）用 `memory_write` 写入，判据见下表；一次性、会话内临时信息只存在于会话。

新记忆与已有记忆有因果/派生关系时用 `memory_link` 双向连上，检索时自动带出邻居（两条记忆必须同 ns，跨 ns 链被拒绝；私有 `agent-*` ns 的两条记忆须带 `agent`＝本宿主 source id，仅属主可连）。邻居是线索不是结论：采纳以 hit 本身为准。

## 写入约定

| 项 | 约定 |
|---|---|
| `type` | `fact` 客观事实（配置、账号、环境参数）；`insight` 经验教训；`skill` 可复用操作方法；`episode` 事件经历；`decision` 已做的选择（选型、方案拍板，长寿如 fact，被新决策取代走冲突裁决） |
| `key` | fact/insight/skill/decision 用稳定英文短横线标识（`user-tts`、`proj-xxx`），格式 `^[a-z0-9]+(-[a-z0-9]+)*$`，`write` 落库前校验（不合规 ValueError）；**禁止日期前缀**——id 已含日期，日期化 key 天然一次性，等于放弃同 key 更新通道（2026-10-05 单日多会话沉淀出成批日期 key 的教训）；更新既有事实复用同 key，新版本与旧版内容不同时返回 `conflict: true` 并入冲突队列 |
| `source` | 宿主标识：`agent-workbuddy` / `agent-zcode` / `agent-claude` / `agent-cursor` / `agent-deepseek`。必须与宿主 env `COMPOUND_MEMORY_AGENT_ID` 一致 |
| `content` | 上限 262144 字符；超限 `ValueError`（`content exceeds`），不落盘。长材料改成短事实或只存指针 |
| `ns` | 默认 `_shared`；`agent-*` 是私有区，写/读/反馈都只认属主——读私有 ns 须带 `reader`（自己的 agent id，缺省即拒绝），越权抛 `PermissionError`；ns 只允许 `[A-Za-z0-9_-]`（路径组件安全，含 `../`、`/`、`*` 等一律 ValueError——ns 会被直接拼进存储路径）。`memory_search` 不传 `ns` 时自动并搜自有私有区（双通道，见必做动作①） |
| `valid_from` / `valid_until` | 可选 ISO 日期（YYYY-MM-DD）标注事实有效期；`valid_until` 已过的事实自动退出检索结果（`memory_get` 仍可读）。事实会过时的场景（负责人变更、配置轮换）写新版时带上预期失效日，过期后检索不再被旧值污染 |
| `project` | 可选项目作用域 slug（`^[a-z0-9]+(-[a-z0-9]+)*$`，与 key 同格式，落库前校验）：标注后该记忆只对声明同一项目的会话检索可见（未标注 = 全局通用）。宿主使用规则声明「本工作区 project=<slug>」后，该工作区的 `memory_write` / `memory_search` 每次携带；CLI 另有 `COMPOUND_MEMORY_PROJECT` env 回退（adapter 层读，store 不读环境变量）。跨项目通用的事实**不要**标 project |
| `lesson_role` | 可选，只有 `lesson` 或 `avoid`；省略 = 普通记忆。不新增类型：lesson 通常写 `insight`（稳态也可写 `decision`），avoid 写 `decision` 并带 `lesson_role: avoid`。不要把角色写进 key。需要并排时，`links` 用前缀 `avoid:` / `fixes:` / `contradicts:` 加目标 id 或 key；其它字符串仍是自由链接 |
| `状态类事实` | 进行时/待办类内容（「剩余待办」「已就绪待…」）易腐：要么带 `valid_until`，要么改写成不含进行时态的稳定事实；写前自问「这条一个月后还成立吗」，拿不准就不写——过时的状态记忆比没有更糟 |
| 内容 | 中文，与库内既有条目一致 |

读取语义：按 id 的 `memory_get` 恒含 `found` 键，目标不存在返回 `{"found": false}`；目标在私有 ns 时必带 `reader`；归档记忆仍可 get，对它 `memory_feedback` 或 CLI `revive`（私有 ns 带 `--reader`）即恢复可检索。

## 教训时机（lesson / avoid）

借用教训语义，不搬因果引擎。不新增记忆类型，不新增 MCP 工具，`suggested_action` 仍只有 merge / promote / review。负向段与矛盾对只报告，不放宽 `dream-apply`。

| 时机 | 动作 |
|---|---|
| 写教训 | 只写稳态，不写草稿。lesson → `memory_write`，type 多用 `insight`（稳态可用 `decision`），`lesson_role: lesson`。avoid → type `decision`，`lesson_role: avoid`。坑与正解并排时，links 写 `avoid:<id 或 key>`、`fixes:<id 或 key>` 或 `contradicts:<id 或 key>` |
| 动手前 | 先 `memory_search`，再对命中 `memory_get`。看 `lesson_role`，并顺着 links 里的 `avoid:` 目标再 get（前缀链不会被邻居召回自动展开）。不要为这一步加工具 |
| 读报告 | `dream` 或 `distill-plan` 的 `negative_salience` 与 `contradiction_pairs`（dream 在 `distill` 段，与 distill-plan 同字段）。高 `failure_count`、低置信、`lesson_role: avoid`、同 key 内容冲突、`contradicts:` 对都在这里。只读 |
| 失败反馈 | 教训误导了你：`memory_feedback`，`outcome: failure` |
| 错教训作废 | 新写纠正正文，再归档源。走既有 `memory_write` / `distill-apply`（或 dream-apply 里**本来就过门槛**的同 key merge）。不要因为负向段或矛盾对放宽 `dream-apply` |

## 运维与蒸馏（CLI）

命令前缀 `uvx --from compound-memory compound-memory`（已 `uv tool install compound-memory` 时可直接写 `compound-memory`；uvx 不在 PATH 时用 `~/.local/bin/uvx`）。从仓库联调才用 `uv run --directory <仓库绝对路径> compound-memory`：

| 命令 | 何时用 |
|---|---|
| `doctor` | 一页健康诊断（安装 / 多宿主自检）。stdout 是 JSON，告警在 stderr。覆盖 `attestation`（`COMPOUND_MEMORY_AGENT_ID` 是否注入）、`retrieval`（`vector` 还是 `lexical`，以及 reason）、`write_lock`（能否加独占锁）、`git`（记忆库是不是 git 仓库、工作树脏不脏）。退出码：`0` 无硬问题；`1` 硬失败（AGENT_ID 未设置或空白、根目录缺失或不是目录、写锁不可用、git 仓库不可用）。词面降级和脏工作树只报告，不因此失败。不创建缺失的根目录，也不会给还不是仓库的目录 `git init`。不新增 MCP 工具 |
| `stats` | 看健康度：uses/confidence 分布、活性、蒸馏产出；`retrieval.mode` 是 `vector` 还是 `lexical`；`attestation` 是否注入了进程身份；`expired_active` 是活动区里 valid_until 已过的条数；`cross_validated` 是 `validated_by` 去重后 ≥2 的条数（一次跨宿主反馈不够）。`COMPOUND_MEMORY_AGENT_ID` 未设置或空白时退出码 `1`（JSON 仍打印）；已设置时退出码 `0`。调用方参数错误仍是退出码 `2` |
| `explain <id> [--reader <agent id>]` | 按 id 看单条记忆的置信度构成（证据计数 success/failure/contradiction、last_verified、最近反馈明细、跨宿主验证明细 validated_by、派生标记、当前 conf）——排查「这条 conf 为什么这么高/低」「这条是不是蒸馏产物」时用；只读不产生提交，私有 ns 加 `--reader` |
| `rebuild-index` | 手工编辑过记忆文件**内容**后（活性检测只覆盖新增/删除文件） |
| `review-queue` / `review-resolve` | 处理同 key 冲突队列与 contradiction 争议（人工裁决入口）：`review-resolve <废置id>` 清行并自动归档废置方（对侧保留活动区）；contradiction 争议行裁决二选一——维持原记忆有效用 `review-resolve <争议id> --uphold`（解冻并按 failure −0.2 折算），确错则缺省路径归档；`--all` 只清空队列不归档（争议行解冻但不折算）。私有 `agent-*` ns 的行仅属主可清（加 `--reader`）——`--all` 会静默保留别人的私有行，显式点名则报错；`review-queue` 展示仍全量 |
| `decay` | 衰减归档，长期未用且少用才动。单独跑，或由 `maintain` 在 `--with-decay` / `RUN_DECAY=1` 时调用；默认的每日维护不跑它 |
| `dream [--ns <ns>] [--reader <id>] [--propose]` | 离线只读编排（ADR-0001，无 LLM）：对一个 ns 出报告。`distill` 段复用 distill-plan（merge_with / possible_dup_of / promotion_candidate / key_duplicates，`suggested_action` 只有 merge / promote / review，以及只读的 `negative_salience` / `contradiction_pairs`）；`decay_signals` 只计数（valid_until 已过、归档区规模、低活性）；`stats_before` / `stats_after` 空跑相等。不写盘、不归档、不 distill-apply、不 forget。负向段不放宽写盘。定时准备跑的就是它，没有 `--apply`。私有 ns 加 `--reader`。显式 `--propose`（默认关，ADR-0011）把过硬门槛的同 key 合并候选写到 `<root>/distill/dream-proposals.json`（形状即 dream-apply 的列表；无合格簇写 `[]`）。系统提议，人工 `dream-apply` |
| `dream-apply --proposals <file> --source <agent> [--dry-run] [--ns <ns>] [--reader <id>]` | 显式半自动落库（#72，无 LLM）。`--proposals` 是 JSON 列表，每项必须自带 `content`、`type`、`source_ids`（可选 `key` / `confidence`；`promote: true` 才允许晋升）。只编排既有 `distill-apply`：同 key 强信号 `merge_with` / `key_duplicates`，以及调用方点名的 `promotion_candidate`。`suggested_action: review`（`possible_dup_of`）只报告不落库。未过门槛的项列入 `skipped` 并给原因。`--dry-run` 只打印 `planned`、零写盘。不 forget，没有 `--include-forget`，也没有 git revert 子命令（回看用 `git-log`）。定时任务禁止调用。私有 ns 加 `--reader`，且 `--source` 必须是属主 |
| `revive <id>` | 复活归档记忆（私有 ns 记忆加 `--reader`） |
| `forget <id> --agent <agent id> [--reason <动机短语>]` | 终态遗忘（ADR-0009）：文件物理移出（活动/归档区皆可）+ 单条 forget 提交留痕，内容仅存 git 历史。不可复活：对被遗忘记忆 feedback/revive 返回 `found: false`，恢复 = 带外 git 运维（checkout/revert）。私有 `agent-*` ns 仅属主可遗忘（`--agent` 与 feedback 同规）；幂等（不存在/已遗忘返回 `found: false`）。`--reason` 是动机短语（单行、限 80 字符），不贴记忆正文。隐私边界：forget 解决「活动库不再携带」，不解决「历史不再包含」——git 历史清理是另行决策的破坏性运维 |
| `git-log` | 审计轨迹（每次写入自动 commit）；消费端降噪：`--grep PATTERN`（可多次，OR）只留消息匹配的提交、`--exclude PATTERN`（可多次）剔除匹配的提交，PATTERN 为正则作用于消息段（剥 hash），如 `git-log --exclude feedback` |
| `health-snapshot` / `health-trend --days N` | 健康时序（#100，CLI-only）：`health-snapshot` 追加一行（date + stats 全量原样）到库根 `health/health.jsonl`——显式运维动词（人工或 launchd/cron 定时；`maintain` 在可选 decay 之后调用它），单写者不持写锁，同日多行、去重归读面；指标单一来源是 stats，不另算；`health/` 不入 git（与 index/ 同型工件）。`health-trend` 读最近 N 天按 date 取每日最后一条，现算总量 / 归档率 / 平均置信度 / review 队列长度 / 蒸馏产出 / 跨宿主覆盖率 / recent_feedback_7d + 首尾 delta；纯文件读不开 store，空态退出码 0。两命令退出码恒 0（身份状态在行内 attestation，定时任务不误报），不是 MCP 工具 |
| `maintain [--with-decay] [--ns <ns>] [--reader <id>]` | 一条日常维护（CLI-only）：顺序固定为只读 `dream`（写入 `<root>/distill/last-plan.json`，不带 `--propose`）→ 可选 `decay` → `health-snapshot`。默认不 decay；`--with-decay` 或 `RUN_DECAY=1`（`DECAY=1` 同义）才衰减，且快照反映衰减后的库。任一步失败非 0 并点名该步。不调用 `dream-apply` / `distill-apply`，也不调用 `skill-export`。`distill-prepare.sh` 仍只跑 dream。不是 MCP 工具 |
| `skill-export [--ns <ns>] [--reader <id>] [--project <slug>] [--min-uses N] [--min-confidence X]` | 只读列出该 ns 里已落盘、可导出的 `type=skill`（ADR-0014）。stdout 是对象 `{ns, candidates, skipped}`，不是裸数组。缺省 `--min-uses 0`、`--min-confidence 0.5`（导出自己的门槛，不读蒸馏的 `PROMOTION_USES_THRESHOLD` 或 `--window`）。不传 `--project` 只收未标注 project 的 skill；传了只收该 slug，不并上全局，也不回退 `COMPOUND_MEMORY_PROJECT`。私有 ns 必须 `--reader`。不写宿主文件，不写 `distill/`，不生成 description。不进定时任务 |
| `skill-export-apply --items <file> --target cursor-project\|cursor-user\|dir` | 只写出 items 点名的条目。items 是 JSON 列表，每项 `id` 与 `key` 二选一，外加单行 `description`（必填，strip 后非空，最长 1024 字符；Cursor 文档没有更小上限，Agent Skills 规范也是 1024）。不要写 `content`：正文用库内 `content` 原文，进程不摘要、不调用 LLM。`cursor-project` 要已存在的绝对 `--workspace`，文件在 `<workspace>/.cursor/skills/<key>/SKILL.md`（不创建工作区本身）。`cursor-user` 写到 `~/.cursor/skills`，不是 `skills-cursor`。`dir` 要绝对 `--out`。相对路径拒绝。`--dry-run` 只给 path / action / sha256，不建目录。没有对应溯源行的已有文件不覆盖，除非 `--replace`。不写记忆库、不 git commit。不进 `maintain` / 定时任务 |

### 抽取清单确认（P0：扫描只发现候选，写库仍走协议）

会话 transcript 经 `extract` 确定性扫描（模式匹配、零 LLM）产出疑似值得沉淀的用户陈述清单。Agent 读清单**逐条判断**：值得写就 `memory_write`（key 自己定、优先复用 `likely_dup_of` 指向的既有条目 key；同 key 冲突照常进 review 队列），不值得就丢弃——扫描器不做丢弃决策，也不直接写库。清单为空属正常（宁缺勿滥）：当前模式表只覆盖中文用户话的声明/踩坑句式，Agent 复述与命令粘贴不在扫描范围。密码、账号句式不进候选；清单落盘前会把密码 / token / 密钥形态打成 `[REDACTED]`（只发生在抽取候选阶段，不改记忆检索和读取）。多句按句切开，一句一个候选。

**WorkBuddy 批量扫描**：`extract ~/.workbuddy/projects` 一次吃全部会话。实测 91 个真实会话 / 200 轮用户话只出 4 条候选——**用户话中位数仅 11 字符，多是任务请求而非陈述**（「帮我做个…」「支持哪些主题」），这是宿主交互形态决定的，不是解析缺陷（解析层已验证召回完整：216 轮全解析）。因此清单天然稀少，不要靠放宽模式表硬凑——宁可空清单，也不要噪声清单。另注两点：`subagents/` 下的 `role:user` 是 team-lead agent 的派活文本（第三人称转述用户），批量模式已跳过；`~/.workbuddy/traces/` **不接入**——trace 的 `toolInput` 被头部硬截到 100000 字符，单快照只剩首轮用户话，接进来是"看似扫过、实则大面积漏"，该目录只作排障线索。

### 蒸馏工作流（判断段归调用方 Agent）

1. `dream`：只读报告写到 `<root>/distill/last-plan.json`。只准备蒸馏时定时任务只跑这条只读命令（`distill-prepare.sh`）。一条 cron 用 `maintain`：先写同一份报告（不带 `--propose`），decay 默认关（`--with-decay` 或 `RUN_DECAY=1` / `DECAY=1` 才跑），然后 `health-snapshot`。`dream-apply` / `distill-apply` 不进定时任务（ADR-0001 / #72 / #97）。`skill-export` / `skill-export-apply` 同样不进定时任务（ADR-0014）：清单 stdout 含正文，写出必须由调用方点名，并且不由 `maintain` 或 `dream` 触发。候选清单在 `distill` 段（与直接跑 `distill-plan` 同字段）：主候选标注 merge_with / possible_dup_of / promotion_candidate，并带 `suggested_action`（只有 **merge** / **promote** / **review**）。`suggested_actions` 把簇列成清单，成员是 `source_ids`。同 key 多版本（`key_duplicates`，含 uses=0 的废置旧版）自动标 merge；高活性 episode 标 promote；`possible_dup_of`（含跨 key 弱相似）标 review。同段还有只读的 `negative_salience` 与 `contradiction_pairs`（教训负向与矛盾对，不产生新动作，也不放宽 dream-apply）。没有 synthesize，也不写 `supersedes:`——跨 key 多源若你判断该综合，动作名仍是 merge，簇就是你给出的 `source_ids`，溯源靠产物 links。`decay_signals` 只是计数线索，归档仍走 `decay` / `distill-apply` / `review-resolve`，forget 仍是显式 CLI。扫私有 ns 加 `--reader`；`distill-apply` 的源与产物必须同 ns。只要候选清单时可直接 `distill-plan`。
2. Agent 读报告的 `suggested_action` 做取舍、自己写产物正文。判断段不在 server 内（无 LLM，不改写磁盘上的记忆正文）。没有产物正文时不要落库。
3. 按动作落库，仍走既有写路径。**merge**：用簇里的 `source_ids` 写一条综合正文，`distill-apply "<产物>" <type> <source> --sources <id1>,<id2>`（同 key 组也可放进 dream-apply，正文仍要你给）。**promote**：写成更高 type 的新记忆，`dream-apply` 的该项必须带 `promote: true`，或直接 `distill-apply`；类型不原地改。**review**：留在报告里，不要放进 dream-apply。若判断后仍要综合，改按 merge 调用 `distill-apply`。要按门槛收一批：先 `dream-apply --proposals <file> --source <本宿主> --dry-run` 看 `planned` / `skipped`，确认后再去掉 `--dry-run`。产物 links 溯源到源、源归档可复活。任意会话发现清单有新候选时按需处理即可。

## 故障排查

| 症状 | 处置 |
|---|---|
| 宿主看不到 5 个 memory_* 工具 | 手动跑 `uvx --from compound-memory compound-memory-server` 看报错：多为 uvx 不在 PATH（`command` 写 `~/.local/bin/uvx` 绝对路径）。从仓库联调时才检查 `--directory` 是否指向克隆目录 |
| 搜索为空 / 召回不全 | `stats` 看记忆量与 `retrieval`；`mode=lexical` 表示当前是纯词面（缺 vec extra/模型，或当次向量召回失败）。怀疑索引损坏 `rebuild-index`（缓存可随时重建，检索降级不报错） |
| MCP `memory_search` 的 `retrieval.mode` 为 `lexical` | 结果质量是 BM25，不是向量。补 `--extra vec` 与本地模型后 `rebuild-index`；`reason` 为 `degraded` 时先看 server 日志里的 `vector recall degraded to lexical` |
| 检索排序不符合预期 / 想知道某条为什么排前 | `memory_search` 传 `explain: true`（CLI `--explain`）看每条 hit 的排序分量（词面/向量 rank、RRF 分、先验折算项、检索通道）与证据摘要；单条记忆的置信度构成用 CLI `explain <id>`（证据计数、跨宿主验证明细、派生标记） |
| SessionStart 没注入 | hook 任何异常都静默退出；手动跑 `~/.agents/memory/hooks/session_start.py` 查输出是否为合法 `{"additionalContext": ...}` JSON |
| 写入/读取/反馈 `PermissionError` | ns 越权：日常写读 `_shared`；私有 `agent-*` ns 的读/反馈带 `reader`/`agent`、link 带 `agent`（`agent-<名>` 或 `<名>`） |
| 报 `contradicts attested agent` | 宿主已注入进程身份（`COMPOUND_MEMORY_AGENT_ID`），自报身份与之矛盾：`source`/`agent` 改填自己的 agent id，`reader` 可直接省略（自动补真值）；仍报错则核对宿主 env 配置 |
| `stats` / `doctor` 退出码 1，或日志出现 `COMPOUND_MEMORY_AGENT_ID is unset` | 宿主 env 未注入进程身份（未设置或空白）。共享 `_shared` 必须补上稳定的 `COMPOUND_MEMORY_AGENT_ID` 后重启 server。这是 CLI 健康检查的硬失败；MCP 五个工具不因此中断，但未注入时调用方可以冒充其他 agent |
| `doctor` 报 `root` / `write_lock` / `git` | 根目录不存在先 `init`；写锁加不上是另一进程持锁或锁文件创建失败；git 不可用表示没有审计仓库（git 不在 PATH、没有 `.git`、或 `git status` 失败）。脏工作树只提示 |
| `write` 报 `key must match` | key 格式不合规（禁大写/下划线/空格/日期前缀）：改用小写字母数字段以短横线连接（`proj-xxx`）；key 是同 key 更新的锚点，日期化会让事实更新退化成不断新增 |
| 写入返回 `conflict: true` | 内容与既有版本不同，已入冲突队列；裁决后 `review-resolve <废置id>` 清行并自动归档废置方（索引同步、自动 commit）。不要手改 frontmatter 再搬文件。`--all` 只清行不归档（无裁决信息） |
| 项目记忆检索不到 | 先核对调用方 `project` 参数（或 CLI `COMPOUND_MEMORY_PROJECT`）与记忆标注一致；检索缺省 fail-closed 只见全局记忆（ADR 0010），声明对应项目才见 全局 ∪ 该项目；按 id 的 `memory_get` 不受 project 限制，可先取回核对 frontmatter |
| 报 `project must match` | project slug 格式不合规（禁大写/下划线/空格/连续横线）：与 key 同格式，改用小写字母数字段以短横线连接 |

架构与复利机制见仓库 `README.md`，术语见 `CONTEXT.md`。
