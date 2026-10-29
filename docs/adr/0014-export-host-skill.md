# 从库内 type=skill 导出宿主 SKILL.md：调用方点名，正文用已有 content，默认不写

本文件只记录设计，供后续实施 PR 按文末验收落地。本 PR 不改运行时，不加 CLI，不加 flag，不改 `TYPE_SPEC`，不改 `distill_plan` / `dream` / `dream_apply` / `maintain`，不改 MCP tool 清单，不改版本号。

对齐 ADR-0001：server 零 LLM；判断留在调用方；定时任务与 `maintain` 不静默落任何产物。对齐 ADR-0013：库内 `type=skill` 的晋升路径已经落地（#116 决议、#117 实现）。本决议只加一条**向外**的路：把已经落在库里的 skill 记忆写成宿主能加载的 `SKILL.md`。不回头改 ADR-0013 的验收条文。

**三套文件，名字都带 skill，不是同一件东西。**

| 叫什么 | 落在哪 | 谁的决议 |
| --- | --- | --- |
| 库内 skill | `namespaces/<ns>/skill/<id>.md`，`type=skill` | ADR-0013 负责怎么写进库。本决议只读，不新写记忆 |
| 宿主 skill 文件 | 目录加一份 `SKILL.md`，宿主按 frontmatter 的 `name` / `description` 决定何时加载 | 本决议 phase 2 的 v1 要写的就是这个 |
| 本仓库规范 skill | `skills/compound-memory/SKILL.md`（使用规范权威源），以及 `integrations/{claude,workbuddy,zcode}/skills/compound-memory/SKILL.md`（薄包装，指向规范源） | 产品自己的接入说明。不是某条用户记忆的导出 |

DeepSeek Harness（dsh）今天没有 plugin skill 文件，规则在 agent 指令里（`docs/agent-integration.md` §5）。不在本决议的写出范围内。

## 与 ADR-0013 的关系

ADR-0013 的非目标写了「宿主 `SKILL.md` 与 plugin skill 文件的生成或改写（phase 2）」。本文件就是那一阶段的范围。它不改这些已经生效的行为：

- 只有 `suggested_action` 为 `promote` 的报告行带 `target_type=skill`。`merge` / `review` 不带。
- 同 ns 已有活动 skill 且 key 相同，则该晋升行改为 `review`，不带 `target_type`。
- `dream-apply` 在 `promotion_candidate` 且 `type=skill` 且该 key 已有活动 skill 时跳过，不调用 `distill_apply`。`content`、`key`、`type=skill` 仍由调用方在 apply 时提交。`promote: true` 才放行晋升。
- `dream` / `dream --propose` / `maintain` 不写步骤正文。`dream-proposals.json` 仍只有合并包。
- `TYPE_SPEC["skill"]` 保持 #117 落地后的值：`ttl_days` 为 `None`，`tau_days` 为 `365.0`，`weight` 为 `0.9`，`key_conflicts` 为 true。
- `maintain` 仍是不带 `--propose` 的 dream → 可选 decay → health-snapshot。

`target_type=skill` 是蒸馏报告上的标注，不是导出候选。导出候选只认已经落盘的记忆：`type=skill` 的文件。报告行、episode 原文、`dream-proposals.json` 里的合并正文，都不进导出。

实施 PR 不改 `docs/adr/0013-maintain-extract-skill.md` 的验收清单，也不把导出门槛塞回 `PROMOTION_USES_THRESHOLD` 或 `suggested_action`。

## 触发

两条命令分开。`skill-export` 只列候选，不写宿主文件。`skill-export-apply` 才写，而且只写调用方在 items 文件里点名的条目。没有「把本次候选全部写出」的缺省。

一次调用只处理一个 ns。`--ns` 缺省 `_shared`。不走检索的双通道（`_shared` ∪ 私有 ns）：导出是点名，不是 `memory_search`。私有 `agent-*` 必须带 `--reader`，属主门禁走现有的 `validation.check_ns_owner`（等价形式 `agent-x` / `x` 的归一与进程身份矛盾时的拒绝，沿用现有身份裁决）。`_shared` 不需要 `--reader`。`--project` 若出现，先过 `validation.check_project`。

**候选谓词**（全过才进入 `candidates`）。时钟用 store 注入的 `today()`，测试钉 `CLOCK_DATE`，不读墙钟。

```text
eligible(mem) =
    mem.type == "skill"
    and mem.ns == <本次 --ns>
    and not mem.archived
    and scoring.recency_age(mem, today) is not None
    and (valid_until 缺省，或是可解析的 ISO 日期)
    and not scoring.is_expired(mem, today)
    and mem.content.strip() != ""
    and mem.key 匹配 storage.validation._KEY_RE
    and project 轴按下节，取精确相等，不用检索并集
    and mem.uses >= min_uses                 # 缺省 0
    and mem.confidence >= min_confidence     # 缺省 0.5
    and mem.id 不在 ReviewQueue.pending_contradictions()
```

然后按 key 分组。某一 key 在通过上式的记忆里不是恰好 1 条，则这几条全部离开 `candidates`，原因 `key_not_unique`。同 key 多条活动 skill 时不挑赢家（`key_conflicts` 让第二条进 review 队列，队列裁决前两边都还在活动区）。

**project 轴不复用 `visibility.in_project_scope`。** 那个函数是检索语义：未标注 project 的记忆对任何读方可见，传了 project 则见「全局 ∪ 该项目」。宿主 skill 文件一旦写出，宿主不会再按记忆库的 project 过滤。因此：

- 不传 `--project`：只收 `project` 为空的记忆。带 project 的 skill 不进这份清单。
- 传 `--project <slug>`：只收 `project` 等于该 slug 的记忆。全局 skill 不混进这次导出。

不调用 `is_retrievable` 充当导出谓词。归档与 `valid_until` 已过仍用上面的 `archived` 与 `scoring.is_expired`（当日仍有效，次日起算已过；坏日期不冒充已过）。ns 与 project 按本节，不按检索并集。

**活性不含蒸馏窗口。** 不设 `window_days`，不读 `dream` / `distill-plan` 的 `--window`。skill 的 `ttl` 是 `None`、`tau_days` 是 365，30 天窗口会把仍有效的 skill 藏掉。新近基准只用来丢掉坏日期（`recency_age` 为 `None`），不用来设上限。

**uses 缺省 0，confidence 缺省 0.5。** 这两个数字写在导出命令自己的缺省上，不从 `distill-plan` 引。以后改蒸馏缺省，不自动改导出缺省。

`uses` 不设下限，是因为新写入的 skill（`memory_write` 或 `distill_apply`）起点就是 `uses=0`：`Memory` 的 uses 缺省为 0，蒸馏产物的证据块还是 ADR-0008 的显式零起点。uses 要等 `memory_feedback` 才增加。缺省 1 会把刚按 ADR-0013 落库、还没有反馈的 skill 挡在导出之外。调用方要更严，就显式传 `--min-uses`（例如 5，与 `PROMOTION_USES_THRESHOLD` 同数，但是另一次选择，不把那个常量接进来）。

`confidence` 缺省 0.5，与写入缺省相同。一次 failure 折算 −0.2 之后会落到线下，这种 skill 不进缺省候选。要连这些一起看，显式把 `--min-confidence` 调低。不用 `PROMOTION_USES_THRESHOLD` 当导出门：那道门已经管过「episode 要不要标成 skill」。

`skill-export` 的 stdout 是一个 JSON 对象，不是裸数组：`ns`、`candidates`、`skipped`。`candidates` 每项至少含 `id`、`ns`、`key`、`project`、`uses`、`confidence`、`content`（返回正文，所以本命令先过上面的 ns 属主门禁）。`skipped` 只列该 ns 里 `type=skill`、但没进候选的记忆，每项一个原因。其它 type 不出现。一条记忆只给一个原因，优先级从先到后：

`archived`，`bad_date`，`valid_until_passed`，`empty_content`，`missing_key`，`bad_key`，`project_mismatch`，`below_uses`，`below_confidence`，`pending_contradiction`，然后第二遍才是 `key_not_unique`。

`skill-export` 不写 `<root>/distill/`，不写 proposals 文件，不改 `last-plan.json`。description 是判断，进程填不出来，所以没有 `skill-export --propose`。

## 正文谁来写

宿主文件的步骤正文是库内 `content` 的原文，前后只加下面这一层确定的壳。进程不摘要、不翻译、不把 episode 拼成步骤、不调用 LLM。调用方也不在导出时再交一份正文：正文在写入这条 skill 记忆时已经由调用方给出（`memory_write`，或 ADR-0013 的 `dream-apply` / `distill-apply`）。导出不调用这两个 apply，也不改记忆文件。

壳的形状（frontmatter 两个键，顺序固定为 `name` 然后 `description`；用 YAML 转义，不手写未转义拼接）：

```yaml
---
name: <key>
description: "<调用方给的单行描述>"
---
<!-- compound-memory-export: id=<id> ns=<ns> key=<key> -->

<content 原文>
```

- `name` 等于记忆的 `key`。目录名也是这个 key。
- `description` 只来自 apply 的 items 文件。必填，strip 之后非空，单行，最长 1024 字符。空串、换行、超长都是 items 形状错误，整份列表在写出前拒绝。1024 不是宿主实测上限，只是挡住把正文再贴进 description。实施时若所写宿主的文档给出更小上限，取更严的那个，并在实施 PR 写明出处。不从正文首行截一段来冒充 description——何时加载是判断，留在调用方。
- 溯源注释是紧挨 frontmatter 结束之后的那一行，格式逐字如上（一个空格的分隔，没有别的键）。幂等判断只认这一行，不在正文里搜索同样的字样。
- 正文与 `content` 逐字节相同。仅当 `content` 末尾不是 `\n` 时，文件末尾补一个 `\n`，使写出的是文本文件。
- 记忆的 frontmatter（confidence、evidence、source、links、lesson_role 等）不抄进宿主文件。溯源注释只带 `id`、`ns`、`key`。
- `content` 自身若以 `---` 开头，仍当作正文，不与壳的 frontmatter 合并。

v1 不给记忆模型加 `description` 字段。宿主触发句只活在这次导出的 items 里，不进记忆 frontmatter，避免把「库内正文」和「宿主何时加载」合成一个字段。

items 文件是 JSON 列表，由调用方自己写。每个元素恰好两个键：`id` 与 `key` 二选一，外加 `description`。缺键、两个标识都写、`content`、其它多余键、空 description、description 含换行或超过 1024 字符，整份列表 `ValueError`，一个文件都不写。出现 `content` 表示调用方以为导出可以改正文。同一份列表里两个元素落到同一个目标路径，同样在写之前拒绝整份列表。库内状态不合格（谓词、`foreign_file`、`path_blocked`）才是逐条 `skipped`，不是整份拒绝。

## 写出到哪里

没有缺省目标。`skill-export-apply` 不带 `--target` 是调用方错误（退出码 2），零写。`--workspace` 与 `--out` 必须是绝对路径；相对路径拒绝。拒绝的原因：本仓库里 cwd 会在调用之间漂移。

phase 2 的 v1 只有三个目标。

| `--target` | 根目录 | 每个 key 的文件 |
| --- | --- | --- |
| `cursor-project` | `<--workspace>/.cursor/skills` | `<root>/<key>/SKILL.md`。`--workspace` 必填，且必须已存在；不创建工作区本身 |
| `cursor-user` | `Path.home() / ".cursor" / "skills"` | 同上。这是用户 skill 目录。`~/.cursor/skills-cursor` 是 Cursor 自带 skill，v1 不写那里 |
| `dir` | `<--out>` | `<--out>/<key>/SKILL.md`。`--out` 必填。给人手工拷进某个宿主目录用 |

三个目标的文件形状相同。v1 不探测 Claude、WorkBuddy、ZCode、dsh 各自的用户 skill 目录，也不把 project slug 映射到工作区路径（库里没有这张表）。调用方自己把 `--workspace` 指到要放的工作区。

**v1 明确不写的路径**（即便 `--out` 指过去也拒绝）：

- 解析后的目标文件落在记忆库根（`COMPOUND_MEMORY_ROOT` / `--root`）之内。
- 路径任一组件是 `integrations`（marketplace plugin 树，含 `integrations/{claude,workbuddy,zcode}/skills/`）。
- 路径组件以 `skills` / `compound-memory` / `SKILL.md` 结尾，且 `skills` 的前一个组件不是 `.cursor`。这挡住本仓库规范源 `skills/compound-memory/SKILL.md`。`cursor-project` 下的 `.cursor/skills/compound-memory/SKILL.md` 不受这条影响：那是用户自己的 key，不是规范源。

写出前对目标 `resolve()`，结果必须仍在该 `--target` 的根目录之内。key 已是 `_KEY_RE` 的单路径组件，仍做这道包含检查（2026-10-05 ns 穿越那类问题：落点先 resolve 再写）。符号链接若指到根外，拒绝。目标路径的父级若已存在且不是目录，该条 `path_blocked`，不写出。

**以后再议，不在 v1：** Claude / WorkBuddy / ZCode / dsh 的用户级或 marketplace 目录自动选路；把导出结果提交进插件目录并改 plugin 版本；覆盖规范源。这些要另一次决议。v1 的 `dir` 目标已经够把文件交到人手里。

## 命令与默认关

新的 CLI 动词，不扩展 `dream-apply` / `distill-apply`。那两个动词写的是记忆库里的新记忆，并会归档源。导出写的是记忆库外面的宿主文件，不归档、不新写记忆。放进 dream-apply 会让一个动词有两种副作用，也会让 `maintain` 长出导出 flag。ADR-0011 的 proposals 文件继续只放合并包；ADR-0013 已经拒绝把 skill 行写进那个文件。本决议不重开。

与 `health-snapshot` 一样，这是 CLI-only 运维动词：不进 `server.py`，不进 `MemoryStore` 的写动词表（write / feedback / link / decay / revive / forget / review-resolve / batch / distill_apply）。读走现有的按 id / 按 ns 扫描，先过身份门禁。

| 命令 | 行为 |
| --- | --- |
| `skill-export` | 只读清单，stdout 是上一节的 JSON。不创建目录，不写宿主文件 |
| `skill-export-apply --items <file> --target <三选一>` | 按 items 点名写出。每条在写之前重新读库并重跑候选谓词；清单和批准之间库变了，不再合格的进 `skipped`，原因 `not_candidate`。不信任一份过期清单 |

`maintain`、`dream`、`dream --propose`、`scripts/daily-maintain.sh`、`scripts/distill-prepare.sh` 都不调用这两条命令，也不增加 `--export` / `--propose` 一类 flag。只读的 `skill-export` 同样不进定时任务：它的 stdout 含记忆正文，cron 会把正文打进日志。

`--dry-run` 只在 `skill-export-apply` 上。它解析目标、读已有文件以便分类，但不创建目录、不写文件。stdout 里每条有 `path`、`action`（`create` / `update` / `unchanged` / `skip`）、`id`、`key`；将要写出的带上内容的 sha256，不把正文再打一遍（正文用 `get` 或 `skill-export` 看）。

全部条目都被跳过仍是退出码 0，结果在 JSON 的 `skipped` 里。调用方错误（参数、相对路径、坏 JSON、非法 ns、items 形状、目标越界、私有 ns 越权）走现有 CLI 翻译：`ValueError` / `PermissionError` → stderr JSON，退出码 2，且在任何写出之前返回。

合格与跳过混在同一次 apply 里时，合格的照写（与 dream-apply 的部分成功相同）。同路径冲突是写前拒绝，不是部分成功。写出顺序按 key 稳定排序。中途 IO 失败时，已经替换完成的文件保留，报告里点名失败项；不回滚宿主目录，也不在记忆库 git 里补提交。

导出本身不 `git commit`（记忆库或工作区都不提交）。宿主文件要进版本库由人自己交。

## 幂等与覆盖

分类只看目标 `SKILL.md`，不动同目录里的其它文件。v1 不删除宿主文件：记忆被归档、遗忘、或跌出置信度门槛之后，已经写出的 `SKILL.md` 留在原地。没有 unexport。

| 目标文件现状 | 行为 |
| --- | --- |
| 不存在 | `create`。只创建 `<key>/` 与该 `SKILL.md` |
| 存在，溯源行的 `id` 等于本条，且即将写出的字节与现文件相同 | `unchanged`。不写 |
| 存在，溯源行的 `id` 等于本条，字节不同（正文或 description 变了） | `updated`。覆盖。库内这条记忆是该 id 的源 |
| 存在，没有本格式的溯源行，或 `id` 是另一条记忆 | 不覆盖。`skipped`，原因 `foreign_file`。手写 skill（例如仓库里的 `.cursor/skills/verify-feedback-write-lock/SKILL.md`）因此不会被撞掉 |

`--replace` 缺省关。加上之后，允许覆盖 `foreign_file`。它不解上面的路径拒绝（规范源、`integrations/`、记忆库根、根外），也不跳过候选谓词。

写出用目标目录内的临时文件再替换，避免留下半截 `SKILL.md`。不把宿主目录加进记忆库的 git。

## MCP

`server.py` 仍是恰好这 5 个 tool：`memory_write`、`memory_search`、`memory_get`、`memory_link`、`memory_feedback`。

不加第六个。导出要写记忆库根以外的路径，还要调用方给 description 和目标目录。MCP 边界今天只碰记忆库；把宿主文件系统写进 tool，等于扩大 server 的写权限，也把「5 个 tool 是记忆读写边界」拆掉。低频、带路径副作用、必须人点名的动作放 CLI，与 forget、dream-apply、health-snapshot 同一侧。

本决议的 PR 不改 `skills/compound-memory/SKILL.md`、`docs/agent-integration.md`、`docs/agent-integration.en.md`、spec。动词还不存在，先写进使用规范会变成空文档。实施 PR 按仓库约定，把这两条命令补进规范 skill 的 CLI 表、`docs/agent-integration.md` 与 spec。那是在登记新动词，不是把某条记忆导出成规范源。实施 PR 不改 `integrations/*/skills/**`，不改 plugin 版本，不发 PyPI。

## 验收（实施 PR）

- [ ] `skill-export` 对给定 ns 只把通过候选谓词、且该 key 恰好一条的活动 skill 放进 `candidates`。`uses=0` 且 `confidence=0.5` 的新写入 skill 在缺省下进候选。归档、坏日期、`valid_until` 已过、空正文、缺 key、非法 key、project 不精确匹配、uses 低于显式 `--min-uses`、confidence 低于缺省 0.5、pending contradiction、同 key 多于一条，都在 `skipped` 里且各只一个原因。stdout 是带 `candidates` 的对象。该命令不创建宿主文件，不写 `distill/`。
- [ ] 不传 `--project` 时不列出带 project 的 skill；传了 `--project` 时不列出全局 skill，也不列出其它 project。私有 ns 缺 `--reader` 或 reader 不是属主时拒绝；`_shared` 不要求 reader。
- [ ] `skill-export-apply` 不带 `--target`、或 `--workspace` / `--out` 为相对路径时，退出码 2，零写。`--dry-run` 给出 path / action / sha256，且不创建目录。
- [ ] 写出的 `SKILL.md`：`name` 为 key，`description` 为 items 里的字符串，溯源行紧跟 frontmatter，其后正文等于该记忆的 `content`（仅允许补一个结尾换行）。记忆文件、索引、记忆库 git 不因这次导出而变。进程内无 LLM。
- [ ] 同一 id、同一 description、内容未变的再次 apply 为 `unchanged`。同一 id 在 description 或 `content` 变化后为 `updated`。没有对应溯源行的已有文件保持原样，除非 `--replace`。
- [ ] `cursor-user` 的根是 `~/.cursor/skills`，不是 `skills-cursor`。`cursor-project` 必须有已存在的绝对 `--workspace`。目标 resolve 后若落在记忆库根、任一 `integrations` 组件下、或规范源 `skills/compound-memory/SKILL.md`（前一个组件不是 `.cursor`），拒绝且不写。
- [ ] items 里缺 `description`、description 为空或含换行、出现 `content` 或其它多余键、或两条落到同一路径：写之前拒绝整份列表。点名的 id 在 apply 时不再满足候选谓词则 `not_candidate`，不写该条。
- [ ] `maintain`、`dream`、`dream --propose`、`dream-apply`、`distill-apply` 的行为与 ADR-0013 落地后一致。`daily-maintain.sh` 与 `distill-prepare.sh` 不调用导出。MCP tool 仍是上面那 5 个。
- [ ] 实施 PR 同步规范 skill 的 CLI 表、`docs/agent-integration.md` 与 spec。不改 ADR-0013 正文的验收清单，不改 `integrations/**` 的 skill 文件，不改 plugin 版本，不改包版本，不发 PyPI。

## 非目标

第六个 MCP tool，或给现有 tool 加导出参数。进程内 LLM，或由 `skill-export` / `dream` / `maintain` 生成 description 或步骤正文。`skill-export --propose`。把导出接进 `dream-apply`、`distill-apply`、`dream-proposals.json` 或 `maintain`。定时任务调用 `skill-export` 或 `skill-export-apply`。

写或覆盖 `skills/compound-memory/SKILL.md`。写 `integrations/**`。自动发布到 Claude / WorkBuddy / ZCode / dsh 的 marketplace。dsh agent 指令文件。探测并写入各宿主用户级 skill 目录（v1 只有 Cursor 的两个固定根，加上显式 `--out`）。把 project slug 映射到磁盘路径。写入 `~/.cursor/skills-cursor` 或 `~/.agents/skills`。

unexport：归档、遗忘、置信度下跌时删除已写出的宿主文件。把宿主文件上的手工修改同步回记忆库（方向只是库 → 文件）。给记忆 frontmatter 增加 `description`。导出 `type` 不是 skill 的记忆。用 `window_days`、`PROMOTION_USES_THRESHOLD` 或 ADR-0011 的 `PROPOSE_MIN_*` 当导出门槛。用检索的「全局 ∪ project」并集当导出的 project 轴。

改 ADR-0013 的验收、`TYPE_SPEC`、晋升标注或 `suggested_action` 词汇。版本号与 PyPI。在记忆库或工作区里为导出自动 `git commit`。
