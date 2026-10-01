# Spec: 本地多 Agent 共享记忆系统（compound-memory）

标签：`ready-for-agent`
状态：已定稿（基于 2026-10-01 设计讨论直接综合，未做追加访谈）
实现状态：2026-10-01 全量审核后与代码同步；未实现条目以「roadmap」内联标注，其余描述与实现一致。

---

## Problem Statement

我在本地同时使用多个 AI Agent（WorkBuddy/TARS、Claude Code、Cursor、各种脚本），每个 Agent 的记忆彼此隔离：TARS 学到的经验 Claude Code 用不上，Claude Code 踩过的坑 Cursor 还要再踩一遍。更糟的是，现有记忆大多是"写完就死"的一次性存储——没有使用反馈、没有强化、没有沉淀机制，记了等于白记。我需要一套本地优先的共享记忆系统，让所有 Agent 共用同一份记忆，并且这份记忆越用越值钱（复利），而不是越积越乱。

## Solution

一套本地多 Agent 共享记忆系统（compound-memory）：

- **统一协议**：一个 Memory MCP Server 作为唯一读写入口，任何支持 MCP 的客户端（或通过 CLI）都能接入，存储层对 Agent 透明。
- **文件即数据库**：纯 Markdown + YAML frontmatter + Git 存储，人可直接读改、可审计、可回滚；向量/关键词索引只是可重建的缓存。
- **命名空间**：`_shared` 共享区（复利发生地）+ `agent-*` 私有区（草稿/偏好），写权限隔离。
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
- **协议契约**：MCP server 暴露且仅暴露 5 个 tool——`memory_write` / `memory_search` / `memory_get` / `memory_link` / `memory_feedback`。`memory_feedback` 是一等公民而非可选项，这是复利闭环的关键约束。
- **命名空间模型**：`_shared` 全 Agent 可读写；`agent-<name>` 仅 owner 可写，读不隔离（本地单机可信环境，读写两侧均不校验读取者身份）。写入必须带 `source`（写入者标识，用于跨 Agent 验证与审计）。
- **数据模型**：每条记忆为一个 md 文件，frontmatter 字段：`id / ns / type / source / created / confidence / uses / last_used / links / ttl / key / validated_by / archived / origin`。`type ∈ {episode, fact, insight, skill}`，type 决定写入策略（episode 为 append-only）、衰减窗口（episode 90d / insight 180d / fact 与 skill 不衰减）与蒸馏去向；`origin` 为可选字段，仅蒸馏产物携带 `distillation`（由 distill-apply 写入）。
- **评分与置信度公式**（来自设计讨论，已与用户对齐）：
  ```text
  检索得分 = 0.45·相似度（BM25 词面）+ 0.25·置信度 + 0.20·新近度(e^(−Δt/τ)) + 0.10·类型权重
  置信度   = min(1, conf₀ + 0.1·uses + 0.15·跨Agent验证次数)
  ```
- **复利四来源**：① 使用强化（feedback 回写 uses+1、conf+0.1）② 关联增值（links 双向关联，memory_get 时带出一度邻居；search 命中自动内嵌精简邻居——每 hit 上限 3、一度、去环、只召回活动记忆，邻居不参与排序分，include_neighbors/--no-neighbors 可关）③ 蒸馏提纯（`distill-plan` 确定性扫描产出带信号标注的候选清单 → 调用方 Agent 判断取舍/摘要 → `distill-apply` 原子落库，条数减少密度上升）④ 跨 Agent 验证（与 source 不同的 agent feedback 时，conf+0.15）。
- **防通胀**：新近度指数衰减 + 长期未用且少用（uses < 3）的记忆归档（不物理删除）+ 蒸馏时双信号去重标注（key 强信号 + BM25 弱信号，只标注不合并，合并与否由判断段裁决）。
- **冲突解决**：episodes append-only 天然无冲突；facts/insights 同 key 不同值时保留双版本并生成 review 队列，由主治 Agent 或人裁决；一切写入带 source + 时间戳。
- **生命周期状态机**：类型终身不变（episode/fact/insight/skill 原地不迁移，改类型 = 蒸馏新写 + 源归档）；强化由 uses/confidence 表达（不设 reinforced/principle 中间类型）；晋升 = 蒸馏产物（高活性 episode 在 distill-plan 标 promotion-candidate，判断后置给 Agent 蒸馏为更高密度新记忆）；任意记忆可经衰减进入 archive，archive 命中可复活并按新证据重算 conf。
- **索引即缓存**：记忆文件本身可直接 ripgrep；词法索引（token→路径缓存）可随时从源文件重建，SQLite 不作为主存储；向量索引（sqlite-vec）deferred——触发条件为活动记忆 ≥500 条或实际报告 search 召回缺口，届时重开；技术路线已验证（sqlite-vec wheel + BGE-small-zh ONNX int8 + onnxruntime 1.19.2 + RRF rank 融合）。
- **Git 集成**：每次写入自动 commit；仓库仅留本地或推私有 remote。
- **技术选型**：Python（managed runtime 3.13）实现 stdio MCP server；蒸馏任务由系统 cron 或宿主 automation 调度（未实现，roadmap）。
- **落地节奏**：P0 纯文件约定 + ripgrep 检索脚本（半天）→ P1 MCP server + 向量索引（1–2 天）→ P2 复利引擎：feedback 闭环 + 定时蒸馏 + 衰减归档（2–3 天）。P2 之前只是"开户"，复利从 P2 开始。

## Testing Decisions

- **测试缝两层**：MCP tool 边界（test_mcp_tools.py，`mcp.Client(server)` 内存直连、无 stdio 子进程——anyio 限制要求 client 会话与测试同 task）+ 核心模块单测（scoring / index / model / store 运维面）。评分公式在 MCP 边界经 search 排序间接断言，公式内部不单测。
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
