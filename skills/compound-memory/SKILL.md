---
name: compound-memory
description: 本机跨 Agent 共享记忆库 compound-memory 的使用规范：接到非琐碎任务先 memory_search 查记忆；采纳记忆后 memory_feedback 强化；任务结束沉淀稳定事实（type/key/source 约定）；跑记忆库运维或蒸馏（stats/decay/revive/review-queue/distill）；检索、写入或注入异常时排查。
---

# compound-memory 使用规范

本机跨 Agent 共享记忆库：MCP 服务 `compound-memory`（5 个工具是唯一读写边界），存储 `~/.agents/memory`，CLI 做运维与蒸馏。接入配置（各宿主 MCP 注册）见仓库 `docs/agent-integration.md`。

## 三个必做动作（复利闭环）

1. **任务开始先检索**：接到非琐碎任务，先 `memory_search` 按任务关键词查相关记忆（用户偏好、项目背景、环境坑）。
2. **采纳即反馈**：命中且**实际采纳**后必须调 `memory_feedback`（`agent` 填本宿主 source id）——复利闭环的核心动作，漏掉它记忆库就不增值。归档记忆被 feedback 自动复活。
3. **任务结束沉淀**：会话确认的稳定事实（用户偏好、项目约定、环境限制、踩坑结论）用 `memory_write` 写入，判据见下表；一次性、会话内临时信息只存在于会话。

新记忆与已有记忆有因果/派生关系时用 `memory_link` 双向连上，检索时自动带出邻居。邻居是线索不是结论：采纳以 hit 本身为准。

## 写入约定

| 项 | 约定 |
|---|---|
| `type` | `fact` 客观事实（配置、账号、环境参数）；`insight` 经验教训；`skill` 可复用操作方法；`episode` 事件经历 |
| `key` | fact/insight 用稳定英文短横线标识（`user-tts`、`proj-xxx`）；更新既有事实复用同 key，新版本与旧版内容不同时返回 `conflict: true` 并入冲突队列 |
| `source` | 宿主标识：`agent-workbuddy` / `agent-zcode` / `agent-claude` / `agent-deepseek` |
| `ns` | 默认 `_shared`；`agent-*` 是私有区仅属主可写（越权抛 `PermissionError`） |
| 内容 | 中文，与库内既有条目一致 |

读取语义：按 id 的 `memory_get` 恒含 `found` 键，目标不存在返回 `{"found": false}`；归档记忆仍可 get，对它 `memory_feedback` 或 CLI `revive` 即恢复可检索。

## 运维与蒸馏（CLI）

命令前缀 `uv run --directory <仓库> compound-memory`（`<仓库>` 见宿主 MCP 配置的 `--directory` 参数；uv 不在 PATH 时用 `~/.local/bin/uv`）：

| 命令 | 何时用 |
|---|---|
| `stats` | 看健康度：uses/confidence 分布、活性、蒸馏产出 |
| `rebuild-index` | 手工编辑过记忆文件**内容**后（活性检测只覆盖新增/删除文件） |
| `review-queue` | 处理同 key 冲突队列（人工裁决入口） |
| `decay` | 衰减归档，长期未用且少用才动（定时任务跑） |
| `revive <id>` | 复活归档记忆 |
| `git-log` | 审计轨迹（每次写入自动 commit） |

### 蒸馏工作流（判断段归调用方 Agent）

1. `distill-plan`：确定性候选清单写到 `<root>/distill/last-plan.json`（launchd 每天 09:00 自动跑），标注 merge_with / possible_dup_of / promotion_candidate。
2. Agent 读 `last-plan.json` 做取舍、拟合并文案。
3. `distill-apply "<产物>" insight <source> --sources <id1>,<id2>`：原子落库，产物 links 溯源到源、源归档可复活。任意会话发现清单有新候选时按需处理即可。

## 故障排查

| 症状 | 处置 |
|---|---|
| 宿主看不到 5 个 memory_* 工具 | 手动跑启动命令看报错：多为 uv 不在预期路径，或 `--directory` 指向的仓库位置漂移 |
| 搜索为空 / 召回不全 | `stats` 看记忆量；怀疑索引损坏 `rebuild-index`（缓存可随时重建，检索降级不报错） |
| SessionStart 没注入 | hook 任何异常都静默退出；手动跑 `~/.agents/memory/hooks/session_start.py` 查输出是否为合法 `{"additionalContext": ...}` JSON |
| 写入 `PermissionError` | ns 越权：日常写 `_shared` |
| 写入返回 `conflict: true` | 内容与既有版本不同，已入冲突队列；裁决后把废置版本归档（frontmatter `archived: true` 移入 `archive/` 并删活动文件）再 `rebuild-index` |

架构与复利机制见仓库 `README.md`，术语见 `CONTEXT.md`。
