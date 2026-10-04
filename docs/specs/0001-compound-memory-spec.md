# Spec: 本地多 Agent 共享记忆系统（compound-memory）

标签：`ready-for-agent`
状态：已定稿（基于 2026-10-01 设计讨论直接综合，未做追加访谈）
实现状态：2026-10-03 与代码同步（向量索引已落地，见「索引即缓存」）；评分公式于 2026-10-03 经 vec-spike 实测重定权。

---

## Problem Statement

我在本地同时使用多个 AI Agent（WorkBuddy/TARS、Claude Code、Cursor、各种脚本），每个 Agent 的记忆彼此隔离：TARS 学到的经验 Claude Code 用不上，Claude Code 踩过的坑 Cursor 还要再踩一遍。更糟的是，现有记忆大多是"写完就死"的一次性存储——没有使用反馈、没有强化、没有沉淀机制，记了等于白记。我需要一套本地优先的共享记忆系统，让所有 Agent 共用同一份记忆，并且这份记忆越用越值钱（复利），而不是越积越乱。

## Solution

一套本地多 Agent 共享记忆系统（compound-memory）：

- **统一协议**：一个 Memory MCP Server 作为唯一读写入口，任何支持 MCP 的客户端（或通过 CLI）都能接入，存储层对 Agent 透明。
- **文件即数据库**：纯 Markdown + YAML frontmatter + Git 存储，人可直接读改、可审计、可回滚；向量/关键词索引只是可重建的缓存。
- **命名空间**：`_shared` 共享区（复利发生地）+ `agent-*` 私有区（草稿/偏好），读写权限隔离。
- **复利引擎**：记忆通过四个机制增值——使用强化、关联召回、周期蒸馏、跨 Agent 验证；同时用衰减 + 归档防通胀。核心信念：**复利 = 反馈闭环，没有 feedback 的记忆都是死本金。**

## User Stories

1. As an AI Agent, I want 将工作中学到的经验写入共享记忆, so that 下次会话不必从零开始。
2. As an AI Agent, I want 按语义相关度检索历史记忆, so that 站在过去的经验上工作而不是重复劳动。
3. As an AI Agent, I want 在采纳某条记忆后回写使用反馈, so that 被验证过的记忆变得更可信、排序更靠前。
4. As an AI Agent, I want 在新记忆与旧记忆之间建立关联, so that 检索命中时能一并召回相关上下文。
5. As an AI Agent, I want 读取共享区中其他 Agent 写入的记忆, so that 享受其他 Agent 经验带来的复利。
6. As an AI Agent, I want 拥有私有命名空间存放未验证的草稿, so that 不污染共享区的信噪比。
7. As an AI Agent, I want 知道每条记忆的写入者与置信度, so that 能评估该不该信任这条记忆。
8. As an AI Agent, I want 通过 links 拉取一条记忆的关联邻居, so that 快速重建一个主题的完整上下文。
9. As a 用户, I want 用任意 MCP 客户端挂载同一份记忆库, so that 不被任何单一工具锁定。
10. As a 用户, I want 直接用编辑器打开并修改记忆文件, so that 人可以审计、纠错、手工整理。
11. As a 用户, I want 每次写入都有 Git 提交记录, so that 误删误改可以回滚，多 Agent 写入有审计轨迹。
12. As a 用户, I want 冲突的事实进入 review 队列而不是被静默覆盖, so that 我（或主治 Agent）能做最终裁决。
13. As a 用户, I want 蒸馏的确定性准备（候选扫描/信号标注/归档）定时自动运行、判断（摘要/合并）由 Agent 按需完成, so that 不需要我手工整理记忆。
14. As a 用户, I want 长期未用且低置信的记忆自动衰减归档, so that 检索质量不被噪声稀释。
15. As a 用户, I want 归档的记忆可以恢复、再次命中时按新证据重算置信度, so that 数据只归档不丢失。
16. As a 用户, I want 索引目录可以随时删除重建, so that 索引损坏永远不会丢失真实数据。
17. As a 用户, I want 记忆数据全部保存在本地, so that 隐私可控、不依赖外部服务。
18. As a 维护者, I want 新 Agent 接入只需声明一个命名空间, so that 接入成本接近零。
19. As a 维护者, I want 通过 CLI 脚本执行全部读写操作, so that 不支持 MCP 的工具也能参与。
20. As a 维护者, I want 查看记忆库健康度统计（uses/confidence 分布、蒸馏产出量）, so that 评估复利引擎是否真的在运转。
21. As a 维护者, I want 不同 Agent 独立复用同一事实时其置信度自动跳升, so that 跨 Agent 验证不需要人工标注。

## Implementation Decisions

- **总体架构四层**：Agent 层（任意 MCP 客户端/CLI）→ 协议层（Memory MCP Server，stdio）→ 存储层（Markdown + frontmatter + Git，位于 `~/.agents/memory`）→ 策略层（评分排序、使用强化、衰减淘汰、定时蒸馏）。
- **协议契约**：MCP server 暴露且仅暴露 5 个 tool——`memory_write` / `memory_search` / `memory_get` / `memory_link` / `memory_feedback`。`memory_feedback` 是一等公民而非可选项，这是复利闭环的关键约束；闭环铁律（采纳后必须 feedback、只写稳定事实、复用既有 key）内嵌在各 tool 的 description 中，使宿主不注入外部使用规范也能维持闭环（注入规范仅用于收紧写入质量）。
- **命名空间模型**（2026-10-03 修订，废止原「读不隔离」决策）：`_shared` 全 Agent 可读写；`agent-<name>` 仅 owner 可写，读同样按属主校验——`memory_search`（显式传私有 ns 时）与 `memory_get` 须带 `reader`（`agent-<name>` 或 `<name>`，缺省即拒绝，fail-closed），`_shared` 读不校验。旁路同规则收口：`memory_link` 只允许同 ns（跨 ns 链会把对侧 id 写进另一侧文件，成为私有 id 泄漏源，且邻居召回本就同 ns 过滤）；`memory_feedback` 对私有记忆仅属主可反馈；`distill_apply` 源与产物必须同 ns；`get` 返回的 `links` 按同 ns 脱敏（兼容存量跨 ns 链）。**检索缺省双通道**（2026-10-03，同日第二次修订）：`memory_search` 不传 `ns` 时作用域为 `_shared` ∪ 调用方自有私有 ns（身份经 attestation 或 `reader` 已知时；未知则退化为单 `_shared`，与旧版一致）——私有条目天然出现在默认检索里，不依赖调用方记得显式补搜（此前两次因漏搜私有 ns 答错自身称呼）；显式传 `ns` 恒为单 ns 精确语义（显式 `_shared` 即不含私有），词面/向量/邻居三路候选同用该作用域集合。身份为自报字符串（本地单机协作边界，非安全边界，细粒度 ACL 仍列非目标）。写入必须带 `source`（写入者标识，用于跨 Agent 验证与审计）。身份证明分层（2026-10-03）：宿主可经 `COMPOUND_MEMORY_AGENT_ID` 环境变量向 server/CLI 进程注入身份（`MemoryStore(agent_id=...)`，store 自身不读环境变量以保测试确定性）——注入后调用方自报身份缺省自动补真值、等价形式（`agent-x`/`x`）归一化为进程身份、矛盾响亮拒绝，`_shared` 的 source 伪造同步关闭；未注入则保持自报模式。
- **数据模型**：每条记忆为一个 md 文件，frontmatter 字段：`id / ns / type / source / created / confidence / uses / last_used / links / ttl / key / validated_by / archived / origin / valid_from / valid_until`。`valid_from`/`valid_until` 是可选的 ISO 日期有效期标注（2026-10-04，Zep 式时态的轻量版——不做图、不做双时态索引）：valid_until 当日仍有效、次日起退出检索候选（词面/向量/邻居三路同排除，蒸馏候选同跳过，stats 以 `expired_active` 计数），get 恒可读；事实更替仍走 review 队列裁决，有效期只决定检索可见性，不做自动失效改写。`type ∈ {episode, fact, insight, skill}`，type 决定写入策略（episode 为 append-only）、衰减窗口（episode 90d / insight 180d / fact 与 skill 不衰减）与蒸馏去向；`origin` 为可选字段，仅蒸馏产物携带 `distillation`（由 distill-apply 写入）。
- **评分与置信度公式**（2026-10-03 vec-spike 实测后重定权，已与用户对齐）：
  ```text
  检索得分 = 0.70·相似度 + 0.15·置信度 + 0.10·新近度(0.5+0.5·e^(−Δt/τ)) + 0.05·类型权重
  置信度   = min(1, conf₀ + 0.1·uses + 0.15·跨Agent验证次数)
  ```
  设计约束：sim 是主序，先验只做 tie-break——conf/recency/type 三槽的有效分差跨度必须盖不过 sim 槽的单 token 命中差，否则高置信/新近的无关记忆会挤掉正确答案（recall-audit 失效模式②的马太效应，spike S1–S4 形态实测复现）。新近度带 0.5 底座（槽内跨度压到 0.5），坏日期记中性值 0.5。双路（向量路启用）时改走 S5 形态：score = RRF_norm + 0.04·(0.5·conf + 0.3·recency_norm + 0.2·type)，score 上限 ≈1.04（不再恒 ≤1）；单路降级时保持上行线性公式。
- **复利四来源**：① 使用强化（feedback 回写 uses+1、conf+0.1）② 关联增值（links 双向关联，memory_get 时带出一度邻居；search 命中自动内嵌精简邻居——每 hit 上限 3、一度、去环、只召回活动记忆，邻居不参与排序分，include_neighbors/--no-neighbors 可关）③ 蒸馏提纯（`distill-plan` 确定性扫描产出带信号标注的候选清单 → 调用方 Agent 判断取舍/摘要 → `distill-apply` 原子落库，条数减少密度上升）④ 跨 Agent 验证（与 source 不同的 agent feedback 时，conf+0.15）。
- **防通胀**：新近度指数衰减 + 长期未用且少用（uses < 3）的记忆归档（不物理删除）+ 蒸馏时双信号去重标注（key 强信号 + BM25 弱信号，只标注不合并，合并与否由判断段裁决）。
- **冲突解决**：episodes append-only 天然无冲突；facts/insights 同 key 不同值时保留双版本并生成 review 队列，由主治 Agent 或人裁决；一切写入带 source + 时间戳。
- **生命周期状态机**：类型终身不变（episode/fact/insight/skill 原地不迁移，改类型 = 蒸馏新写 + 源归档）；强化由 uses/confidence 表达（不设 reinforced/principle 中间类型）；晋升 = 蒸馏产物（高活性 episode 在 distill-plan 标 promotion-candidate，判断后置给 Agent 蒸馏为更高密度新记忆）；任意记忆可经衰减进入 archive，archive 命中可复活并按新证据重算 conf。
- **索引即缓存**：记忆文件本身可直接 ripgrep；词法索引（token→路径缓存）与向量索引（sqlite-vec vec0 表，`index/vectors.db`）都是可随时从源文件重建的缓存，SQLite 不作为主存储。两份缓存同守不变量：活动记忆必被索引、归档记忆必不在索引、缺失/损坏自动重建、检索降级不报错；带外增删的自愈分两档——词法缓存全量重建（纯 tokenize，廉价），向量缓存增量对账（按 content_hash 逐条 diff，只编码新增/变更/移除的条目——跨进程小写入不放大成全库重编码，读路径首查延迟从 O(全库) 降为 O(变更条数)）。向量路为可选能力（extra `vec`：onnxruntime 1.19.2 钉版/macOS x64 上限 + tokenizers + numpy + sqlite-vec），未安装或 BGE 模型缺失（HF 缓存无 `Xenova/bge-small-zh-v1.5`，经 hf-mirror.com 下载，不自动联网）时自动降级纯词面。检索形态：候选 = 词面命中 ∪ 向量 KNN（ns/活性过滤，池 16），两路 rank 在 `rank` 内 RRF（k=60）融合为主序，先验（conf/recency/type）整体压到 ε=0.04 做 tie-break——先验翻不过 rank 差（recall-audit 失效模式②的根治）。写入路径同步编码单条（实测 ~400ms/850 字符）；feedback 内容不变零编码。换 embedding 模型需显式 `rebuild-index`。模型 repo id 与输出维度可经环境变量覆盖（`COMPOUND_MEMORY_EMBEDDING_MODEL`，默认 `Xenova/bge-small-zh-v1.5`；`COMPOUND_MEMORY_EMBEDDING_DIM`，默认 512；单一定义点 `embedding.py`，换模型须同步改维度），支持语言中性/多语言模型接入。
- **Git 集成**：每次写入自动 commit；仓库仅留本地或推私有 remote。
- **技术选型**：Python（managed runtime 3.13）实现 stdio MCP server；蒸馏准备定时调度三选一——macOS launchd LaunchAgent（`scripts/` 安装物，睡眠错过补跑）、Linux systemd user timer（`Persistent=true` 同样补跑）、cron（最通用但不补跑），三者共用平台无关的 `scripts/distill-prepare.sh`；判断段由调用方 Agent 按需完成。
- **抽取管线（P0，2026-10-04）**：会话 transcript → 确定性扫描（模式匹配，零 LLM）→ 候选清单（`extract/last-candidates.json`，运行时工件不入审计史）→ Agent 逐条确认走既有 memory_write。蒸馏三段式的第二应用——扫描只发现候选、写库权留在协议层（不静默原则不变）；清单为一次性快照（下次扫描覆盖），不做处理状态登记；去重标注用查询 token 覆盖率（normalized BM25 对长句查询结构性偏低）。模式表面向中文宿主用户话（statement→fact / pitfall→insight），Agent 复述与命令粘贴不扫；召回不足时先扩模式表，再考虑加一次廉价 LLM 分类（仍不做写库决策）。
- **抽取管线接入宿主会话日志（2026-10-04，四宿主全覆盖）**：transcript 形态按内容自动判别（不靠文件名、不只看首行——较新会话以 `session-meta`/`mode` 等元事件开头，只探首行会静默丢掉最近的会话）。四个受支持源：① **session log**（`~/.workbuddy/projects/<project>/<sessionId>.jsonl`，WorkBuddy 主源，完整逐轮 `type=message`/`role=user`/`input_text` 块，真实用户话被 `<user_query>` 或 `<session>` 包裹，剥壳后再判注入）；② **ZCode 会话库**（`~/.zcode/cli/db/db.sqlite`，全量历史；SQLite `message`+`part` 表，`mode=ro` 只读打开，`json_valid` 守卫坏行——SQL 侧 `json_extract` 遇非法 JSON 会抛错而非返回 NULL，正文在 part 的 text 块，`synthetic`/`model-only` 注入块滤除）。ZCode 最初的 model-io jsonl 快照源已退役：`rollout/` 只剩最近几个会话的 API 快照，接快照属"看似扫过、实则冰山一角"的假阴性，与 trace 同判 unsupported；③ **Claude Code session log**（`~/.claude/projects/<project>/<sessionId>.jsonl`）：真实输入 = `type=='user'` 的 message.content（字符串或 text 块），滤 `isMeta`（UI 回显/命令展开）、`isSidechain`（子 agent 转述）、`<command-*>`/`<local-command-stdout>` 包装与 `[Request interrupted]` 提示；④ **DeepSeek Harness session**（`~/.dsh/sessions/<project>/<会话>/session.jsonl.zstd`，zstd 压缩 JSONL，经系统 zstd CLI 解压免引 C 扩展）：真实输入 = `user/message` 且 `data.source.kind=='user'`（runtime-context/技能注入/审批通知走别的 source.kind，结构上分离），`session.origin=='subagent'` 子会话整场返回空。**dsh 有两代文件名**（`session.jsonl.zstd` / `session.v3.jsonl.zstd`，事件形态相同，实测 25 个里 14 个是 v3）——批量 glob 只认旧名会静默漏掉一半会话，与 WorkBuddy session-meta 教训同型。**`~/.workbuddy/traces/` 刻意不接入**：generation span 的 `toolInput` 被头部硬截到 100000 字符，单快照只剩首轮 user 消息，即便逐条 `raw_decode` 抢救（实测 845/845 span 成功）也属"看似扫过、实则大面积漏"的假阴性——该目录只作排障线索。批量模式传目录（三种布局各自适配，混布局目录也可），跳过 `subagents/`（其 `role:user` 是 team-lead agent 的派活文本，第三人称转述用户，实测候选 3/3 全噪声）。**宿主交互形态决定召回上限**：WorkBuddy 91 个真实会话 / 202 轮用户话只出 4 条候选（全是既有条目源头句），ZCode 全量库 327 轮出 16 条（多为既有记忆源头句），Claude 14 轮 0 条、dsh 55 轮 3 条——用户话中位数短、多为任务请求而非陈述，解析层已验证完整（非缺陷），故不放宽模式表硬凑，宁可空清单不要噪声清单。
- **落地节奏**：P0 纯文件约定 + ripgrep 检索脚本（半天）→ P1 MCP server + 向量索引（1–2 天）→ P2 复利引擎：feedback 闭环 + 定时蒸馏 + 衰减归档（2–3 天）。P2 之前只是"开户"，复利从 P2 开始。

## Testing Decisions

- **测试缝两层**：MCP tool 边界（test_mcp_tools.py，`mcp.Client(server)` 内存直连、无 stdio 子进程——anyio 限制要求 client 会话与测试同 task）+ 核心模块单测（scoring / index / vector_index / model / store 运维面）。评分公式在 MCP 边界经 search 排序间接断言，公式内部不单测。向量路测试用确定性词袋 fake embedder（不依赖 onnxruntime/真模型）；真模型端到端靠 CLI 冒烟与 `experiments/vec-spike/` 回归。
- 好测试的标准：只验证"写入 → 检索 → 反馈"等外部可见行为闭环，例如：写入后 search 能召回；feedback 后同一查询的排序上升；get 能带出 links 邻居；fact 冲突后 review 队列出现双版本。
- 存储与索引是实现细节，但"索引可重建"本身是一条验收测试：删除 index/ 目录后 search 仍正常工作。

## Out of Scope

- 云同步、多机协作、远程访问（本地优先，本期只做单机）。
- 端到端加密、密钥管理。
- Web/GUI 管理界面。
- 嵌入特定 Agent 内部的深度集成（只通过标准 MCP 协议交互）。
- 用 LLM 自动裁决冲突事实（review 队列留给主治 Agent 或人）。
- 写入时的语义去重（去重只发生在蒸馏阶段）。
- 细粒度 ACL/角色权限（命名空间级隔离已够用）。

## Further Notes

- 核心风险：feedback 闭环若不成立（Agent 用完记忆不回写），系统退化为普通笔记库。因此协议设计上 feedback 是独立 tool，且建议接入方在系统提示中强制要求调用。
- 复利的本质是每轮循环抬高"本金质量"（平均密度 + 平均置信度），而非单纯堆量；防通胀与增值同等重要。
- 建议项目名：compound-memory。
- 本 spec 已随仓库纳入版本管理；issue 跟踪走 GitHub Issues（见 docs/agents/issue-tracker.md）。
