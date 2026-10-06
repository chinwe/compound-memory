# compound-memory

本地多 Agent 共享记忆系统（MCP server + CLI）：记忆落盘为带 YAML frontmatter 的 Markdown，复利引擎随使用增值（feedback 强化 / 关联带出 / 跨 Agent 验证 / 衰减归档可复活）。Spec：`docs/specs/0001-compound-memory-spec.md`。

## 常用命令

- 项目用 uv 管理（`pyproject.toml` + `uv.lock`）：首次克隆后 `uv sync --extra dev` 建 `.venv`，日常一律 `uv run`（uv 不在 PATH 时用 `~/.local/bin/uv`）。
- 测试：`uv run pytest tests/ -q`
- 类型检查：`uv run mypy src/compound_memory/`
- CLI 冒烟：`uv run compound-memory stats`；MCP server 启动：`uv run compound-memory-server`（宿主配置见 `docs/agent-integration.md`）。
- 记忆库根目录默认 `~/.agents/memory`，可用 `COMPOUND_MEMORY_ROOT` 覆盖。注意：MemoryStore 每次写入会在记忆库自身的 `.git` 里自动 commit——这是运行时行为，与本仓库的开发 git 无关。

## 架构边界

- `src/compound_memory/` 分层：`server.py`（唯一读写边界，恰好 5 个 MCP tool，勿增删）→ `storage/` 包（ADR 0003：MD+frontmatter 存储、命名空间、git、复利引擎——facade + 机制五件 + 动词七件，见下条）→ 平级领域模块 `index.py` + `scoring.py` + `review_queue.py`（冲突队列 artifact 的生成/解析/清除）+ `vector_index.py`（向量缓存编排：content-hash、embedder 调用、对账 diff、batch 暂存）+ `vector_engine.py`（VecEngine 引擎缝，ADR 0004——引擎只存不算，vec0 虚表/L2→余弦/sqlite 存取在此单点，换引擎实现同一协议即可）+ `embedding.py`（模型缝与编码，可选 vec extra，未装自动降级纯词面）+ `liveness.py`（两份缓存共用的带外增删探测，2026-10-05 收拢——此前两份复制曾漂移出真 bug）+ `extraction.py`（抽取管线确定性段，宿主知识 HOSTS 表驱动）；`model.py` 是共享领域模型（从 storage 拆出以打破循环依赖，勿再引入循环 import）。
- `storage/` 包结构（ADR 0003 终态，#36-#39 逐件外移）：`facade.py` 承载 `MemoryStore`——构造装配、get/link 两个薄动词的方法体（保「动词目录」可读性）、机制薄委托（tests 播种与动词 Deps 的触达面）与其余动词一行转发，公开方法签名即稳定接口；机制层五件 `paths.py`（布局/路径推导/default_root）、`files.py`（save/parse/scan_parsed/find）、`gitlayer.py`（git 子进程/重试/commit/孤儿恢复，GIT_CEILING_DIRECTORIES 钉死仓库发现范围）、`locking.py`（写锁 + batch 协调 + _Batch）、`validation.py`（ns/key/validity 校验 + 门禁谓词 + 身份裁决 + 双正则）；动词层七件 `writing.py`（write/write_new/write_result）、`lifecycle.py`（feedback/decay_sweep/revive/archive/move_to_active）、`search.py`（search/词面候选/向量召回）、`indexing.py`（sync_indexes/scan_pairs/rebuild_index）、`distill.py`、`review.py`、`stats.py`。动词模块各自声明窄 Deps（Protocol，如 DistillDeps），依赖倒置单向：facade→动词模块→Deps，动词不 import facade；动词间依赖链（distill_apply→write_new/archive、review_resolve→find/archive）经 Deps 解，回跳一律经 store 实例属性查找（打桩缝）。无 parity 残留纪律：逻辑单份在模块内、facade 委托仅转发、re-export 声明式——勿留第二份逻辑拷贝。
- 包公开导入面（`storage/__init__.py`，#39 收窄）：仅五个名字 `MemoryStore`/`default_root`/`MEMORY_TYPES`/`DISTILL_DUP_SIM_THRESHOLD`/`PROMOTION_USES_THRESHOLD`（server/cli 消费面）；私有件以模块规范路径为唯一入口——桶函数（`_conf_bucket`/`_uses_bucket` 等）→`storage.stats`、`_unlink_file`→`storage.files`、`GIT_IDENTITY`/`GIT_LOCK_RETRY_DELAYS`/`_git_available`→`storage.gitlayer`、`_Batch`→`storage.locking`、`_PATH_COMPONENT_RE`/`_KEY_RE`/`check_validity`→`storage.validation`、`VEC_POOL`→`storage.search`、生命周期阈值（`ARCHIVE_USES_THRESHOLD`/`CONF_USE_BUMP`/`CONF_CROSS_AGENT_BUMP`）→`storage.lifecycle`。
- 单一定义点，改这些领域前先读对应模块 docstring：
  - `model.TYPE_SPEC`：记忆类型唯一知识源（权重/半衰期/归档 TTL），加类型只改这张表；
  - `scoring.rank`：排序管线与搜索结果形状的唯一位置（权重常量 W_SIM/W_CONF/W_RECENCY/W_TYPE 定义在 scoring.py，改权重只改那里）；
  - `Index`：拥有"活动记忆必被索引、归档必不在索引"不变量，缓存损坏自动重建、检索降级不报错；活性是 store 级的——读路径自动检测跨进程缓存更新（重载）与带外新增/删除文件（目录 mtime 重建，探测共用 `liveness.dirs_newer_than` 单点），手编已有文件**内容**需显式 `rebuild-index`；
  - `MemoryStore.batch`：批量落库正门（逐条校验写穿、批尾一次索引 flush + 一次 commit；失败语义"落地即已提交"，嵌套即 ValueError）。灌库/蒸馏类批量写入一律走它，勿绕过直用 `_save`（experiments 旁路已迁移）；向量侧批尾一次性批量编码，`Index`/`VectorIndex` 的 `defer`/`flush_pending` 仅 batch 调用；锁与 batch 协调体（WriteLocker/_Batch/defer_commit）单点在 `storage/locking.py`；
  - `MemoryStore._write_lock`：写动词（write/feedback/link/decay/revive/review-resolve/batch）的**读-改-写**全程持锁——按 id 读与门禁也在锁内，`find` 在锁外时并发 feedback 同一记忆丢 uses/confidence（2026-10-05 并发测试实证 13/16）。新增写动词先对这条自查；锁本体在 `storage/locking.py`（facade `_write_lock` 是薄委托）；文件写出共用 `atomic_write_text`（index.py，原子替换 + 唯一临时名 + 权限对齐），勿另写 mkstemp；
  - `storage/writing.py` 的 `write_new`：落库核心，write/batch/distill_apply/tests 四方共用（勿再复制第二份落库编排）；
  - `storage/indexing.py` 的 `sync_indexes`：全部写路径的索引收口物理单点（活动必入索引、归档必移出），勿绕过直改两份缓存；
  - `extraction.HOSTS`：宿主 transcript 知识单一定义点（parser / 内容嗅探 / 批量 glob / 提示文案 / CLI 帮助全由表生成），新增宿主 = 一个函数 + 一行表，勿在别处加分支；公共尾部（剥壳滤注入去重）用 `collect_user_texts`；
  - `MemoryStore.lexical_candidates`：公开词面候选通道（返回记忆正文的入口，已过身份门禁，实现在 `storage/search.py`）——extraction 复述标注走它，勿直调 `_candidates` 私有件；
  - `scoring.recency_age`：新近基准（last_used 优先，created 兜底），直接返回距 today 天数、坏日期返回 None；排序与衰减共用，勿各算各的；
  - `ReviewQueue`：review-queue.md 行格式（生成 + 解析 + fail-safe 保留）单一定义点，勿在别处裸读/裸写队列文件；
  - `MemoryStore` 接口错误约定：调用方错误（参数/越权/自链接）抛 `ValueError`/`PermissionError`（CLI/MCP adapter 各翻译一次），目标不存在返回 `{"found": False}`（按 id 动词恒含 `found` 键）；非法 ns（白名单外字符）写/检索抛 `ValueError`，非法 mem_id 在 `find` 视作不存在返回 `None`（调用方对 None 已有容错，抛错会炸掉邻居召回降级，2026-10-05 审计加固）；
  - `_check_ns_owner` + `_resolve_identity`：ns 访问控制与身份裁决唯一位置（谓词定义单点在 `storage/validation.py` 的 check_ns_owner/resolve_identity，执行时序不动：参数型动词在方法入口、按 id 动词在锁内 find 之后——ns 只有 find 后才知道）。不变量：**凡返回记忆正文或元数据的新入口（MCP tool、CLI 命令、store 公开方法）必须先过这两道门**——检索、按 id 读、邻居、蒸馏扫描/落库、复活全覆盖，新增入口先对表自查（2026-10-03 曾靠枚举才发现 distill_plan/revive 两个漏网同族入口）。
- 术语遵循 `CONTEXT.md` glossary，注意每条的 Avoid 列表，不要用同义词漂移。

## 测试与沙箱坑

- 禁用 pytest 内置 `tmp_path`：WorkBuddy 沙箱对已存在目录 mkdir 报 EEXIST，批量 unlink 被 trash hook 拦截。conftest.py 自建 fixture 落到 `.test-tmp/`，新测试直接用现成 fixture。
- anyio cancel scope 不能跨 task：`mcp.Client` 会话须与测试同一 task 内 `async with`（用 asynccontextmanager helper，勿用 fixture 开关 client）。git 可用性经 `MemoryStore(git_probe=...)` / `configure(..., git_probe=...)` 注入，勿 patch 全局 `shutil.which`。
- mcp 2.x 行为：`FastMCP` 已改名 `MCPServer`（`mcp.server.mcpserver`）；单元素 list 返回值会被 unwrap 成对象——批量结果要包一层 `{"hits": [...]}`；工具内异常默认返回 `is_error=True` 而非抛出；tool 一律声明 `structured_output=False`（`dict[str, Any]` 注解会被推断 outputSchema，结构化载荷与文本回退双份下发撑大宿主上下文）。另注意返回形状：CLI `search` 返回裸数组，`{"hits": ...}` 包装只在 MCP 层。
- 删除文件的沙箱约束已收进 seam adapter：生产默认 `Path.unlink`（单文件 unlink 不受批量守卫影响）；conftest 的 `sandbox_safe_remove`（改名 `.{name}.rm`）只在测试侧注入。测试断言日期一律用 conftest 的 `CLOCK_DATE`（store fixture 已注入固定 clock），勿贴真实墙钟。
- 沙箱对后台任务曾有 SIGKILL（exit 137；2026-10-04 一次 17 分钟的**单次巨批** onnx run 被杀，同日一次 45+ 分钟的分块编码任务全程未被杀——疑似与巨批内存峰值有关而非单纯时长）：长编码/评测任务优先分块限内存、被杀后响亮重试；前台 Bash 上限 600s。长命令与后台命令一律绝对路径（cwd 在调用间会漂移，曾把相对路径拼错）。
- 回归测试钉子别用绝对计时断言：沙箱负载波动大（同一提交全量耗时实测 55s~153s），会把「线性但慢」误判成回归（曾把 36.2s 误报给 10s 阈值）。优先结构性计数/不变量断言——如 sys 审计钩子数 tokens 落盘次数与批量大小无关（见 test_batch.py::TestBatchScale）。
- `sys.addaudithook` 钩子内 `event != "..."` 短路必须先于 `args[0]` 索引：注册新钩子会触发无参 `sys.addaudithook` 事件，老钩子先摸 args 抛 IndexError，之后**所有**审计事件静默丢失——症状是「第二个被监测对象计数恒 0」（2026-10-05 五轮探针才定位），与业务代码无关极难排查。
- `search` 默认 `top_k=5`：验证可见性/覆盖面的断言（并发写互见、rebuild 前后对比、灌库全量可检）必须显式放大 top_k 或断言候选集合，否则截断会伪装成「丢更新」——2026-10-05 外部审计的 P1-2 误报与复核第一轮 PoC 双双栽在这里。
- 穿越/路径类 PoC 探针执行前先 `resolve()` 核对落点：`ns` 层级探针会从 `.test-tmp` 写穿到仓库根乃至工作区上层（2026-10-05 曾把 deep_victim/fact/*.md 写进仓库根，幸为探针自建目录可直接清理）。
- git 相关测试制造「坏仓库」时勿用空 `.git` 目录：git 仓库发现会向上借用父链最近的真仓库，测试的 add -A/commit 落错仓（2026-10-05 实测把开发仓库未提交改动 commit 成 "orphan changes recovered"，靠 reset 恢复）；坏仓库一律用 gitfile 形态——`.git` 写成文件 `gitdir: <不存在的路径>`，git 报 fatal 且不逃逸。conftest 的 session 级 HEAD 守卫兜底：会话期间开发仓库 HEAD 移动即炸。生产侧 `_git` 已带 `GIT_CEILING_DIRECTORIES=root.parent` 钉死发现范围（无效 `.git` 报 not a repository 而非逃逸，测试 `TestGitDiscoveryCeiling` 钉住）。
- `tests/contracts/` 契约冻结纪律（#25/#29 裁决）：动词级 characterization 是拆分/重构的安全网，任何搬运片不得修改其断言；搬运中发现契约缺口 = 停下来按「契约变更」流程显式处理，勿顺手改断言迁就实现（#39 后唯一例外是纯 import 路径的机械替换，断言语义一条不动）。
- tests 树无 `__init__.py`，测试模块**基名**必须全局唯一：新测试文件命名前先查基名冲突——batch 契约文件因此叫 `test_batch_channel.py` 而非 `test_batch.py`（与 `tests/test_batch.py` 共存）。

## 约定

- 代码与日志内容用英文；代码注释与 docstring 用中文（与现有代码一致）。
- 敏感区（scoring / model / storage）改动按 TDD 顺序：先写失败测试再写实现（test_scoring.py 即此风格；is_expired 的日期方向错误就是先实现后测试引入的）。
- 改检索、复利、类型生命周期等敏感区前，先读 `docs/specs/0001-compound-memory-spec.md` 与 `CONTEXT.md`。
- 改 tool 行为或使用规则时三处同步：`skills/compound-memory/SKILL.md`（规范权威源，两宿主经软链即时生效）、`docs/agent-integration.md`（工具表与宿主配置）、spec（有设计决策变更时）。

## Agent skills

### Issue tracker

Issues live in the repo's GitHub Issues (`chinwe/compound-memory`), managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role triage vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Privacy gate

Pre-commit + CI 两层门禁拦个人信息入库；禁串在仓库外（本地 patterns 文件 / GitHub Secret），本地 hook 未启用时 CI 仍拦截。改动提交流程或更新禁串前读 `docs/agents/privacy-gate.md`。

### Domain docs

Single-context: root `CONTEXT.md` (glossary) + `docs/adr/`. See `docs/agents/domain.md`.
