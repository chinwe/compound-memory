# 同类 Agent 记忆系统的蒸馏先例（研究笔记）

- Ticket: [001-distillation-precedents](../tickets/001-distillation-precedents.md)
- 分支: `research/distillation-precedents`
- Status: done（结论供票 003 蒸馏执行者、票 004 管线形状使用）
- 方法：只采一手来源（源码 / 官方文档 / 论文 / 官方帮助中心），二手文章仅作导航。所有关键论断标注来源链接。

## 逐家机制

### 1. mem0（开源 memory layer）

一手来源：[`mem0/configs/prompts.py`](https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py)、[论文 arXiv:2504.19413](https://arxiv.org/abs/2504.19413)、[docs.mem0.ai memory operations](https://docs.mem0.ai/core-concepts/memory-operations/update)

1. **触发**：写路径即时——每次 `memory.add()` 都走完整管线，无定时巩固任务。
2. **候选**：两阶段。第一阶段 `FACT_RETRIEVAL_PROMPT` 让 LLM 从新消息对（user+assistant）抽取原子事实（JSON `{"facts": [...]}`）；第二阶段用向量检索找出与这些事实**相似的既有记忆**作为对照集一起送入 `UPDATE_MEMORY_PROMPT`。即候选 = 新事实 + 相似旧记忆，而非全库扫描。
3. **产出**：LLM 输出带事件标签的四操作之一——`ADD`（新 ID 追加）/ `UPDATE`（同 ID 原地改）/ `DELETE`（按输入 ID 删）/ `NONE`。**没有归档层**，DELETE 即从 store 消失；无显式溯源 links。平台版有 memory history API（每次操作的审计轨迹，docs.mem0.ai api-reference）。
4. **去重判据**：纯 LLM 判断，规则写在提示词里：语义重叠但新事实信息更丰富 → UPDATE（"keep the fact which has the most information"）；纯同义重复 → NONE（"Likes cheese pizza" vs "Loves cheese pizza" 不更新）；矛盾 → DELETE。embedding 相似度只用于**选对照集**，不直接判重。
5. **防丢/可撤销**：平台版靠 history 事件留痕；开源版无内建撤销，无 git。
6. **LLM 位置**：抽取与四操作决策全 LLM；确定性部分 = 向量检索对照集、ID 校验（"return the IDs from the input IDs only"）、底层存储。

### 2. Letta（原 MemGPT）：sleep-time agent / dreaming

一手来源：[docs: Memory & dreaming](https://docs.letta.com/configuration/memory/)、[blog: Sleep-time Compute](https://www.letta.com/blog/sleep-time-compute/)、[arXiv:2504.13171](https://arxiv.org/abs/2504.13171)、[forum 最佳实践帖](https://forum.letta.com/t/sleeptime-agents-for-memory-consolidation-best-practices-guide/154)

1. **触发**：异步后台。dreaming（sleeptime subagent）"after a set number of completed agent steps or when the context window is compacted"（完成 N 步或上下文压缩时）；与主 agent 并行、anytime 读写共享记忆，不阻塞对话。
2. **候选**：近期对话（message buffer）+ 当前 memory blocks 状态；不做全量库扫描。
3. **产出**：改写 memory blocks——`memory_rethink`（整块大规模重写，最适合 sleeptime）、insert、replace。**原始对话与 archival memory 不删**（archival 一直是只增的原始层）。记忆落 MemFS：git-backed 记忆文件系统，更新像 git commit。
4. **去重**：LLM 整理，社区最佳实践明确 sleeptime 的职责含 "Reorganize and deduplicate memory blocks"。
5. **防丢/可撤销**：MemFS 的 git 版本化（可 diff/回滚）+ 桌面端 memory viewer 人审 + 可选 "Agent reviews before applying"（第二个后台会话先审查修订再落盘）。
6. **LLM 位置**：主 agent 在对话中做战术性即时写入，sleeptime agent 用 LLM 做深度整理——分工原则 "Primary writes raw, sleeptime refines via rethink"。确定性部分 = 调度触发、block 存储、archival 检索。

### 3. LangMem（LangChain/LangGraph 官方记忆 SDK）

一手来源：[LangMem conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)、[LangChain memory overview](https://docs.langchain.com/oss/python/concepts/memory)

1. **触发**：显式双路径——hot path（对话中即时形成，代价是延迟）与 background（对话后异步反思，"prompting an LLM to reflect on a conversation after it occurs"，召回率更高）。
2. **候选**：对话 + 当前记忆状态。两种载体：**Collections**（无界集合，运行时检索）与 **Profiles**（单一 schema 文档原地更新）。
3. **产出**：统一模式 "Prompt an LLM to determine how to expand or consolidate the memory state"；Collections 必须与新信息和解——"deleting/invalidating or updating/consolidating existing memories"；Profile 天然只保留最新态。记忆分 semantic / episodic / procedural 三策略（procedural 即提示词优化）。
4. **去重**：LLM 驱动（"update or remove outdated memories, and consolidate and generalize"）；官方概念文档**未描述**显式 embedding 阈值判重步骤。检索端（非判重）组合 similarity + importance + strength（recency/frequency 函数）。
5. **防丢/可撤销**：依赖底层 LangGraph store 的可插拔持久化；无内建审计/撤销。
6. **LLM 位置**：提取/更新/删除/巩固全部 LLM 决策；命名空间模板、upsert、检索是确定性管道。

### 4. ChatGPT memory（OpenAI）

一手来源：[help.openai.com: Memory in ChatGPT](https://help.openai.com/en/articles/8590148-memory-in-chatgpt)、[官方公告](https://openai.com/index/memory-and-new-controls-for-chatgpt/)、[系统提示词转储（bio tool）](https://github.com/0xeb/TheBigPromptLibrary/blob/main/Articles/chatgpt-bio-tool-and-memory/chatgpt-bio-and-memory.md)。注：2026-08-18 版 [Model Spec](https://model-spec.openai.com/) 已无独立 memory 工具章节。

1. **触发**：对话中模型自行调用 `bio` 工具（`to=bio`）；无定时巩固任务（saved memories 是平铺短句列表）。2025 年起另有"参考聊天历史"的检索式记忆，不落 saved memory。
2. **候选**：模型在对话中判断值得长期保留的稳定事实/明确偏好；临时会话不写。
3. **产出**：短句条目；多条件**合并更新**而非堆积（存了 "I love dogs" 与 "I love cats" 会合成一条 "User loves dogs and cats"）；注入时置于 system prompt 后的 `# Model Set Context`，每轮刷新。
4. **去重**：模型判断 + 同主题合并。
5. **防丢/可撤销**：完全靠用户控制——Settings > Manage Memory 逐条查看/删除、clear all、temporary chat、对话中让模型 forget；无版本历史。
6. **LLM 位置**：模型即决策者（系统提示词约束"persist information across conversations"）；工具与注入机制是确定性基础设施。

### 5. OpenClaw（原 Clawdbot，开源个人助手）

一手来源：[docs: Memory overview](https://docs.openclaw.ai/concepts/memory)、[issue #43002: Memory Consolidation Mechanism](https://github.com/openclaw/openclaw/issues/43002)

1. **触发**：dreaming 机制默认自动开启，后台运行；正常 heartbeat 提示词本身不做记忆维护；触发点为完成步数/上下文压缩。此外 context compaction 前有 memoryFlush（"silent turn" 先保存重要上下文再摘要，可配置本地模型如 `ollama/qwen3:8b`）。
2. **候选**：**确定性门控选候选**——收集短期召回信号、打分，晋升须过三门：Thresholded（分数 + 召回频率 + 查询多样性）、Taint gated（不可信/系统衍生候选排除）。不是全量扫描也不是纯 LLM 挑选。
3. **产出**：合格条目晋升进 `MEMORY.md`（策划层，"not a raw transcript, daily log, or exhaustive archive"）；`memory/YYYY-MM-DD.md` 日志 append-only 保留；`DREAMS.md` 存 dreaming 扫描摘要**供人审**；`USER.md` 冲突时原地 supersede 而非追加矛盾条目。
4. **去重**：先确定性门、后 "a tool-free completion selects merges and supersessions"——LLM 补全只负责从已过门候选中选合并/取代对，"memory writer composes the result from validated source evidence"（写入器只从已验证源证据组装）。
5. **防丢/可撤销**：源日志永不删、DREAMS.md 留痕、`openclaw memory rem-backfill` 可把旧日课重放回 dreaming store。社区提案（issue 43002，已按此方向实现 dreaming）还提出遗忘卫生："Entries not referenced in N days get confidence-downgraded; Low-confidence entries are **archived (not deleted)**; Frequently referenced entries get confidence-upgraded"。
6. **LLM 位置**：显式声明分层——确定性代码做打分/门控/组装，LLM（可用本地小模型）只做合并/取代的选择与摘要。

### 6. Generative Agents（斯坦福，反思机制——蒸馏的学术原型）

一手来源：[arXiv:2304.03442](https://arxiv.org/abs/2304.03442)（Section 3.2 Reflection / memory stream）

1. **触发**：重要性阈值——最近 100 条观察的 importance 分数（LLM 逐条打 1-10）总和超过阈值（约 150）时触发反思。
2. **候选**：最近 100 条记录 → LLM 生成 3 个最显著问题 → 每问按检索（recency×importance×relevance）取相关记忆。
3. **产出**：每问生成约 5 条 insight，作为**新记忆插入流**（可被后续检索，反思可递归堆叠）；**源观察保留不删**；每条 insight 带 citation 指针（引出它的源记忆索引）。
4. **去重**：无（纯增不减）。
5. **防丢/可撤销**：源记忆保留 + insight 溯源指针；流本身无版本化。
6. **LLM 位置**：importance 打分、问题生成、insight 生成是 LLM；阈值触发、加权检索、指针记账是确定性代码。

## 跨切对比

| 维度 | mem0 | Letta | LangMem | ChatGPT | OpenClaw | Generative Agents |
|---|---|---|---|---|---|---|
| 触发 | 写路径即时 | 异步后台（N 步/压缩） | 双路径（hot+background） | 对话中工具调用 | 后台 dreaming（默认开） | 重要性总和阈值 |
| 候选选择 | 检索相似旧记忆作对照 | 近期对话+blocks | 对话+当前状态 | 模型即时判断 | 确定性三门（分/频/多样性） | 近 100 条+加权检索 |
| 产出/源去向 | 原地改删，无归档 | 改写 blocks，原始层不删 | Collections 和解 / Profile 原地 | 合并更新短句 | 晋升进策划层，日志 append-only | 新增 insight，源保留 |
| 溯源 links | 无（history 审计代替） | git 版本化 | 无 | 无 | DREAMS.md+源证据 | citation 指针 |
| 判重判据 | LLM（四操作） | LLM（rethink） | LLM（consolidate） | LLM（合并） | 确定性门+LLM 选择 | 无 |
| 防丢/撤销 | 平台 history | MemFS git+人审 | store 持久化 | 用户手动管理 | 源不删+归档不删+重放 | 源保留 |

共同模式（六家中至少四家满足）：

1. **巩固与在线路径解耦**：蒸馏在后台/离线跑，不阻塞检索与对话（Letta、LangMem background、OpenClaw、Generative Agents 均异步）。
2. **源原始层保留**：真正被"消化"的是原始层，产出进策划层；没有一家直接删除源数据（mem0/ChatGPT 例外，但它们没有原始层概念）。
3. **embedding/词面相似度只用来选对照集，判重交给判断者**：mem0 检索相似旧记忆送 LLM；OpenClaw 确定性门选候选、LLM 选合并对。没有一家用裸相似度阈值直接判重合并。
4. **防丢三板斧**：版本化（git）、append-only 源层、人审队列（DREAMS.md / Letta review / ChatGPT Manage Memory）。
5. **"写原始、后整理"分工**（Letta "Primary writes raw, sleeptime refines"；OpenClaw 日志 append-only + dreaming 晋升）。

## 对 compound-memory 的借鉴

### 值得直接吸收

1. **双层层级 = 既有模型天然对齐**：OpenClaw 的 `daily logs → MEMORY.md` 与本仓库 `episode → fact/insight` 同构；Generative Agents 的 insight-from-observations 即 `insight` 类型的语义。蒸馏的"日摘要→周洞察→月固化"可落为 `episode → fact → insight` 的类型晋升（票 005 语义），源 episode 归档不删——这正是 OpenClaw issue 43002 Phase 3 与六家中四家的共同选择（归档而非删除）。
2. **确定性门控选候选，判断后置**：OpenClaw 三门（分数/召回频率/查询多样性）映射到本仓库现有信号：`TYPE_SPEC` 权重与置信度（分数门）、feedback 强化次数与 links 度数（频率/多样性门）。门控用 `scoring.rank`/`scoring.recency_age` 同源数据，纯代码零 LLM——server 保持零依赖。
3. **判重 = 检索对照集 + 外部判断者**：mem0 的形状最值得抄：先用现有 BM25 检索取相似旧记忆做对照集（确定性），判重/合并决策作为**指令输出**交给调用方 agent 或人工（模型仅用于判断），而非 server 内嵌 LLM。
4. **蒸馏产物带溯源 links**：Generative Agents 的 citation 指针 → 复用 `memory_link` 把 insight 关联到源 episode；同时天然接通"关联带出"复利来源。归档源记忆保持可复活（现有不变量不被破坏）。
5. **防丢已有地基，补两件事即可**：MemoryStore 写入自动 git commit ≈ Letta MemFS 的版本化；归档可复活 ≈ OpenClaw "archived, not deleted"。需补：蒸馏批次单独 commit 粒度（map 已列微决策）+ 人审通道（DREAMS.md 式的"待审清单"，可挂 CLI 子命令而非新 MCP tool）。
6. **触发时机采 Letta 形状**：完成 N 个事件或容量/压缩信号触发，而非墙钟定时独占；对本仓库即"新增 episode 数达到阈值或 review 后手动"，由 CLI/scheduler 承载（票 008）。

### 不可行 / 需变形

1. **LLM 管线内嵌（mem0/LangMem/Letta 式）不可行**：server 零 LLM 依赖是现状约束。变形：server 只产出**确定性候选集与合并建议清单**（含对照记忆、diff 预览），判断步留给调用方 agent（ZCode 等本就带模型）或 HITL review 队列——即"工具出事实、agent 出判断"。
2. **embedding 相似度判重不可行（暂）**：外部 embedding API 违背本地优先（story 17）；票 002 未决前，判重对照集用 BM25 词面（已有）+ 类型/key 命中（`MemoryStore` 接口已有 key 语义）。若票 002 裁决做本地嵌入，也只是把对照集检索换路，判重仍应后置。
3. **新增第 6 个 MCP tool 不可行**：恰好 5 个是硬约束。蒸馏走 CLI 子命令（`distill`/`consolidate`），产物经既有 `memory_write` 语义落库。
4. **ChatGPT 式"模型对话中即时巩固"需变形**：本仓库写路径是 agent 显式调用，没有会话内自动触点；即时巩固天然属于调用方 agent 的行为（它可边会话边 `memory_write`），服务端只管离线批次。
5. **mem0 式 DELETE 即删不可取**：与本仓库"归档可复活"不变量冲突；蒸馏的"删除"应一律落为归档 + links 保留。
6. **Letta "anytime 并行改写"不必抄**：本仓库无长驻进程，蒸馏是批处理任务，串行足够；抄它的触发信号（步数/容量）即可。

### 给票 003（执行者）/ 票 004（管线形状）的接口建议

- 管线五段：候选门控（确定性）→ 对照集检索（BM25）→ 合并建议清单（结构化输出）→ 判断（调用方 agent / HITL）→ 落库（新 insight + links + 源归档），每段一个检查点。
- 判据词汇沿用本表：ADD/UPDATE/DELETE(NONE) 可借 mem0 四操作作建议清单的事件枚举，但 DELETE 语义在落库层必须翻译为"归档"。
- 防丢验收：任一蒸馏批次可经记忆库 git 单 commit 回滚，源 episode 全部可复活。

## 来源清单

- mem0: https://github.com/mem0ai/mem0/blob/main/mem0/configs/prompts.py · https://arxiv.org/abs/2504.19413 · https://docs.mem0.ai/core-concepts/memory-operations/update
- Letta: https://docs.letta.com/configuration/memory/ · https://www.letta.com/blog/sleep-time-compute/ · https://arxiv.org/abs/2504.13171 · https://forum.letta.com/t/sleeptime-agents-for-memory-consolidation-best-practices-guide/154
- LangMem: https://langchain-ai.github.io/langmem/concepts/conceptual_guide/ · https://docs.langchain.com/oss/python/concepts/memory
- ChatGPT: https://help.openai.com/en/articles/8590148-memory-in-chatgpt · https://openai.com/index/memory-and-new-controls-for-chatgpt/ · https://github.com/0xeb/TheBigPromptLibrary/blob/main/Articles/chatgpt-bio-tool-and-memory/chatgpt-bio-and-memory.md
- OpenClaw: https://docs.openclaw.ai/concepts/memory · https://github.com/openclaw/openclaw/issues/43002
- Generative Agents: https://arxiv.org/abs/2304.03442
