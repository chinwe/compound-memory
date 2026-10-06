---
name: compound-memory
description: 本机跨 Agent 共享记忆库 compound-memory 的使用规范：接到非琐碎任务先 memory_search 查记忆；采纳记忆后 memory_feedback 强化；任务结束沉淀稳定事实（type/key/source 约定）；跑记忆库运维或蒸馏（stats/decay/revive/review-queue/distill）；检索、写入或注入异常时排查。
---

# compound-memory 使用规范

本机跨 Agent 共享记忆库：MCP 服务 `compound-memory`（5 个工具是唯一读写边界），存储 `~/.agents/memory`，CLI 做运维与蒸馏。接入配置（各宿主 MCP 注册）见仓库 `docs/agent-integration.md`。

## 三个必做动作（复利闭环）

1. **任务开始先检索**：接到非琐碎任务，先 `memory_search` 按任务关键词查相关记忆（用户偏好、项目背景、环境坑）。默认检索即双通道：`_shared` + 本宿主私有 ns（身份已知时自动并入，私有条目无需单独补搜）；显式传 `ns` 则只搜该 ns（精确语义）。
2. **采纳即反馈**：命中且**实际采纳**后必须调 `memory_feedback`（`agent` 填本宿主 source id）——复利闭环的核心动作，漏掉它记忆库就不增值。归档记忆被 feedback 自动复活。
3. **任务结束沉淀**：会话确认的稳定事实（用户偏好、项目约定、环境限制、踩坑结论）用 `memory_write` 写入，判据见下表；一次性、会话内临时信息只存在于会话。

新记忆与已有记忆有因果/派生关系时用 `memory_link` 双向连上，检索时自动带出邻居（两条记忆必须同 ns，跨 ns 链被拒绝；私有 `agent-*` ns 的两条记忆须带 `agent`＝本宿主 source id，仅属主可连）。邻居是线索不是结论：采纳以 hit 本身为准。

## 写入约定

| 项 | 约定 |
|---|---|
| `type` | `fact` 客观事实（配置、账号、环境参数）；`insight` 经验教训；`skill` 可复用操作方法；`episode` 事件经历；`decision` 已做的选择（选型、方案拍板，长寿如 fact，被新决策取代走冲突裁决） |
| `key` | fact/insight/decision 用稳定英文短横线标识（`user-tts`、`proj-xxx`），格式 `^[a-z0-9]+(-[a-z0-9]+)*$`，`write` 落库前校验（不合规 ValueError）；**禁止日期前缀**——id 已含日期，日期化 key 天然一次性，等于放弃同 key 更新通道（2026-10-05 单日多会话沉淀出成批日期 key 的教训）；更新既有事实复用同 key，新版本与旧版内容不同时返回 `conflict: true` 并入冲突队列 |
| `source` | 宿主标识：`agent-workbuddy` / `agent-zcode` / `agent-claude` / `agent-deepseek` |
| `ns` | 默认 `_shared`；`agent-*` 是私有区，写/读/反馈都只认属主——读私有 ns 须带 `reader`（自己的 agent id，缺省即拒绝），越权抛 `PermissionError`；ns 只允许 `[A-Za-z0-9_-]`（路径组件安全，含 `../`、`/`、`*` 等一律 ValueError——ns 会被直接拼进存储路径）。`memory_search` 不传 `ns` 时自动并搜自有私有区（双通道，见必做动作①） |
| `valid_from` / `valid_until` | 可选 ISO 日期（YYYY-MM-DD）标注事实有效期；`valid_until` 已过的事实自动退出检索结果（`memory_get` 仍可读）。事实会过时的场景（负责人变更、配置轮换）写新版时带上预期失效日，过期后检索不再被旧值污染 |
| `状态类事实` | 进行时/待办类内容（「剩余待办」「已就绪待…」）易腐：要么带 `valid_until`，要么改写成不含进行时态的稳定事实；写前自问「这条一个月后还成立吗」，拿不准就不写——过时的状态记忆比没有更糟 |
| 内容 | 中文，与库内既有条目一致 |

读取语义：按 id 的 `memory_get` 恒含 `found` 键，目标不存在返回 `{"found": false}`；目标在私有 ns 时必带 `reader`；归档记忆仍可 get，对它 `memory_feedback` 或 CLI `revive`（私有 ns 带 `--reader`）即恢复可检索。

## 运维与蒸馏（CLI）

命令前缀 `uv run --directory <仓库> compound-memory`（`<仓库>` 见宿主 MCP 配置的 `--directory` 参数；uv 不在 PATH 时用 `~/.local/bin/uv`）：

| 命令 | 何时用 |
|---|---|
| `stats` | 看健康度：uses/confidence 分布、活性、蒸馏产出 |
| `rebuild-index` | 手工编辑过记忆文件**内容**后（活性检测只覆盖新增/删除文件） |
| `review-queue` / `review-resolve` | 处理同 key 冲突队列（人工裁决入口）：`review-resolve <废置id>` 清行并自动归档废置方（对侧保留活动区）；`--all` 只清空队列不归档。私有 `agent-*` ns 的行仅属主可清（加 `--reader`）——`--all` 会静默保留别人的私有行，显式点名则报错；`review-queue` 展示仍全量 |
| `decay` | 衰减归档，长期未用且少用才动（定时任务跑） |
| `revive <id>` | 复活归档记忆（私有 ns 记忆加 `--reader`） |
| `git-log` | 审计轨迹（每次写入自动 commit）；消费端降噪：`--grep PATTERN`（可多次，OR）只留消息匹配的提交、`--exclude PATTERN`（可多次）剔除匹配的提交，PATTERN 为正则作用于消息段（剥 hash），如 `git-log --exclude feedback` |
| `extract <transcript\|dir>` | 会话抽取清单（P0）：确定性扫描，候选写 `extract/last-candidates.json`（一次性快照，下次扫描覆盖）。transcript 按内容自动判别四种形态：WorkBuddy session log、ZCode 会话库（`~/.zcode/cli/db/db.sqlite`，全量历史，直接指库文件）、Claude Code session log、DeepSeek Harness session（zstd 压缩，需系统 zstd CLI）。jsonl/zstd 传目录则批量扫（WorkBuddy 与 Claude 同为 `<项目>/<会话>.jsonl`，dsh 为 `<项目>/<会话>/session*.jsonl.zstd`）。`~/.workbuddy/traces/` 与 ZCode rollout/model-io 快照不接入——都只剩部分轮次，接进来是假阴性（理由见下） |

### 抽取清单确认（P0：扫描只发现候选，写库仍走协议）

会话 transcript 经 `extract` 确定性扫描（模式匹配、零 LLM）产出疑似值得沉淀的用户陈述清单。Agent 读清单**逐条判断**：值得写就 `memory_write`（key 自己定、优先复用 `likely_dup_of` 指向的既有条目 key；同 key 冲突照常进 review 队列），不值得就丢弃——扫描器不做丢弃决策，也不直接写库。清单为空属正常（宁缺勿滥）：当前模式表只覆盖中文用户话的声明/踩坑句式，Agent 复述与命令粘贴不在扫描范围。

**WorkBuddy 批量扫描**：`extract ~/.workbuddy/projects` 一次吃全部会话。实测 91 个真实会话 / 200 轮用户话只出 4 条候选——**用户话中位数仅 11 字符，多是任务请求而非陈述**（「帮我做个…」「支持哪些主题」），这是宿主交互形态决定的，不是解析缺陷（解析层已验证召回完整：216 轮全解析）。因此清单天然稀少，不要靠放宽模式表硬凑——宁可空清单，也不要噪声清单。另注两点：`subagents/` 下的 `role:user` 是 team-lead agent 的派活文本（第三人称转述用户），批量模式已跳过；`~/.workbuddy/traces/` **不接入**——trace 的 `toolInput` 被头部硬截到 100000 字符，单快照只剩首轮用户话，接进来是"看似扫过、实则大面积漏"，该目录只作排障线索。

### 蒸馏工作流（判断段归调用方 Agent）

1. `distill-plan`：确定性候选清单写到 `<root>/distill/last-plan.json`（launchd 每天 09:00 自动跑），主候选标注 merge_with / possible_dup_of / promotion_candidate；另有 `key_duplicates` 专项段圈出同 key 多版本组（不受活性门限制——清行未归档的废置旧版 uses=0 进不了主候选），逐组「留新归旧」处置。扫私有 ns 加 `--reader`；`distill-apply` 的源与产物必须同 ns。
2. Agent 读 `last-plan.json` 做取舍、拟合并文案。
3. `distill-apply "<产物>" insight <source> --sources <id1>,<id2>`：原子落库，产物 links 溯源到源、源归档可复活。任意会话发现清单有新候选时按需处理即可。

## 故障排查

| 症状 | 处置 |
|---|---|
| 宿主看不到 5 个 memory_* 工具 | 手动跑启动命令看报错：多为 uv 不在预期路径，或 `--directory` 指向的仓库位置漂移 |
| 搜索为空 / 召回不全 | `stats` 看记忆量；怀疑索引损坏 `rebuild-index`（缓存可随时重建，检索降级不报错） |
| SessionStart 没注入 | hook 任何异常都静默退出；手动跑 `~/.agents/memory/hooks/session_start.py` 查输出是否为合法 `{"additionalContext": ...}` JSON |
| 写入/读取/反馈 `PermissionError` | ns 越权：日常写读 `_shared`；私有 `agent-*` ns 的读/反馈带 `reader`/`agent`、link 带 `agent`（`agent-<名>` 或 `<名>`） |
| 报 `contradicts attested agent` | 宿主已注入进程身份（`COMPOUND_MEMORY_AGENT_ID`），自报身份与之矛盾：`source`/`agent` 改填自己的 agent id，`reader` 可直接省略（自动补真值）；仍报错则核对宿主 env 配置 |
| `write` 报 `key must match` | key 格式不合规（禁大写/下划线/空格/日期前缀）：改用小写字母数字段以短横线连接（`proj-xxx`）；key 是同 key 更新的锚点，日期化会让事实更新退化成不断新增 |
| 写入返回 `conflict: true` | 内容与既有版本不同，已入冲突队列；裁决后 `review-resolve <废置id>` 清行并自动归档废置方（索引同步、自动 commit，无需 rebuild）；`--all` 只清行不归档（无裁决信息） |

架构与复利机制见仓库 `README.md`，术语见 `CONTEXT.md`。
