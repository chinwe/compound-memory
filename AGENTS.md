# compound-memory

本地多 Agent 共享记忆系统（MCP server + CLI）：记忆落盘为带 YAML frontmatter 的 Markdown，复利引擎随使用增值（feedback 强化 / 关联带出 / 跨 Agent 验证 / 衰减归档可复活）。Spec：`docs/specs/0001-compound-memory-spec.md`。

## 常用命令

- 项目用 uv 管理（`pyproject.toml` + `uv.lock`）：首次克隆后 `uv sync --extra dev` 建 `.venv`，日常一律 `uv run`（uv 不在 PATH 时用 `~/.local/bin/uv`）。
- 测试：`uv run pytest tests/ -q`
- 类型检查：`uv run mypy src/compound_memory/`
- CLI 冒烟：`uv run compound-memory stats`；MCP server 启动：`uv run compound-memory-server`（宿主配置见 `docs/agent-integration.md`）。
- 记忆库根目录默认 `~/.agents/memory`，可用 `COMPOUND_MEMORY_ROOT` 覆盖。注意：MemoryStore 每次写入会在记忆库自身的 `.git` 里自动 commit——这是运行时行为，与本仓库的开发 git 无关。

## 架构边界

- `src/compound_memory/` 分层：`server.py`（唯一读写边界，恰好 5 个 MCP tool，勿增删）→ `storage.py`（MD+frontmatter 存储、命名空间、git、复利引擎；蒸馏 distill_plan/distill_apply、衰减 decay、归档/复活也都在这个文件）→ `index.py` + `scoring.py` + `review_queue.py`（冲突队列 artifact 的生成/解析/清除）+ `vector_index.py`/`embedding.py`（向量缓存与编码，可选 vec extra，未装自动降级纯词面）；`model.py` 是共享领域模型（从 storage 拆出以打破循环依赖，勿再引入循环 import）。
- 单一定义点，改这些领域前先读对应模块 docstring：
  - `model.TYPE_SPEC`：记忆类型唯一知识源（权重/半衰期/归档 TTL），加类型只改这张表；
  - `scoring.rank`：排序管线与搜索结果形状的唯一位置（权重常量 W_SIM/W_CONF/W_RECENCY/W_TYPE 定义在 scoring.py，改权重只改那里）；
  - `Index`：拥有"活动记忆必被索引、归档必不在索引"不变量，缓存损坏自动重建、检索降级不报错；活性是 store 级的——读路径自动检测跨进程缓存更新（重载）与带外新增/删除文件（目录 mtime 重建），手编已有文件**内容**需显式 `rebuild-index`；
  - `scoring.recency_age`：新近基准（last_used 优先，created 兜底），直接返回距 today 天数、坏日期返回 None；排序与衰减共用，勿各算各的；
  - `ReviewQueue`：review-queue.md 行格式（生成 + 解析 + fail-safe 保留）单一定义点，勿在别处裸读/裸写队列文件；
  - `MemoryStore` 接口错误约定：调用方错误（参数/越权/自链接）抛 `ValueError`/`PermissionError`（CLI/MCP adapter 各翻译一次），目标不存在返回 `{"found": False}`（按 id 动词恒含 `found` 键）；
  - `_check_ns_owner` + `_resolve_identity`：ns 访问控制与身份裁决唯一位置。不变量：**凡返回记忆正文或元数据的新入口（MCP tool、CLI 命令、store 公开方法）必须先过这两道门**——检索、按 id 读、邻居、蒸馏扫描/落库、复活全覆盖，新增入口先对表自查（2026-10-03 曾靠枚举才发现 distill_plan/revive 两个漏网同族入口）。
- 术语遵循 `CONTEXT.md` glossary，注意每条的 Avoid 列表，不要用同义词漂移。

## 测试与沙箱坑

- 禁用 pytest 内置 `tmp_path`：WorkBuddy 沙箱对已存在目录 mkdir 报 EEXIST，批量 unlink 被 trash hook 拦截。conftest.py 自建 fixture 落到 `.test-tmp/`，新测试直接用现成 fixture。
- anyio cancel scope 不能跨 task：`mcp.Client` 会话须与测试同一 task 内 `async with`（用 asynccontextmanager helper，勿用 fixture 开关 client）。git 可用性经 `MemoryStore(git_probe=...)` / `configure(..., git_probe=...)` 注入，勿 patch 全局 `shutil.which`。
- mcp 2.x 行为：`FastMCP` 已改名 `MCPServer`（`mcp.server.mcpserver`）；单元素 list 返回值会被 unwrap 成对象——批量结果要包一层 `{"hits": [...]}`；工具内异常默认返回 `is_error=True` 而非抛出；tool 一律声明 `structured_output=False`（`dict[str, Any]` 注解会被推断 outputSchema，结构化载荷与文本回退双份下发撑大宿主上下文）。另注意返回形状：CLI `search` 返回裸数组，`{"hits": ...}` 包装只在 MCP 层。
- 删除文件的沙箱约束已收进 seam adapter：生产默认 `Path.unlink`（单文件 unlink 不受批量守卫影响）；conftest 的 `sandbox_safe_remove`（改名 `.{name}.rm`）只在测试侧注入。测试断言日期一律用 conftest 的 `CLOCK_DATE`（store fixture 已注入固定 clock），勿贴真实墙钟。

## 约定

- 代码与日志内容用英文；代码注释与 docstring 用中文（与现有代码一致）。
- 改检索、复利、类型生命周期等敏感区前，先读 `docs/specs/0001-compound-memory-spec.md` 与 `CONTEXT.md`。
- 改 tool 行为或使用规则时三处同步：`skills/compound-memory/SKILL.md`（规范权威源，两宿主经软链即时生效）、`docs/agent-integration.md`（工具表与宿主配置）、spec（有设计决策变更时）。

## Agent skills

### Issue tracker

Issues live in the repo's GitHub Issues (`chinwe/compound-memory`), managed via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role triage vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` (glossary) + `docs/adr/`. See `docs/agents/domain.md`.
