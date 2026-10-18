# 效果核对：能跑，与有效

本页说明怎样核对 compound-memory **接得上**，以及怎样记录它有没有改变**同一道题**的做法。它是清单、一小套固定题、一张记录表，外加一个把 CLI `stats` 存盘的脚本。

这里没有评测框架，没有夜间任务，也没有模型判分。判断留在跑题的人身上。仓库不声称产品已经证明有效；数字只来自按本页填好的记录，来源要能指回命令输出。

检索层的公开基准另在 [`experiments/longmemeval/`](../../experiments/longmemeval/README.md)（recall，零 LLM）。那一组不回答「任务里少踩了没有」。

server 侧没有 LLM（[ADR-0001](../adr/0001-distillation-hybrid-executor.md)）。下面的命令只打 CLI。五个 MCP tool 的名字与参数以 [`server.py`](../../src/compound_memory/server.py) 为准，本页不新造入口。

日常宿主的前缀是 `uvx --from compound-memory compound-memory`。下面用仓库里的 `uv run compound-memory`。两边子命令相同。

核对用独立库，不要写进 `~/.agents/memory`：

```bash
export COMPOUND_MEMORY_ROOT="${TMPDIR:-/tmp}/compound-memory-eval"
export COMPOUND_MEMORY_AGENT_ID=agent-claude
uv run compound-memory init
```

`COMPOUND_MEMORY_AGENT_ID` 未设置或空白时，`doctor` 和 `stats` 退出码是 1，JSON 仍打印。进程身份优先于参数里自报的 `source` / `agent`：两边不一致会 `PermissionError`。跨宿主那一题要换环境变量，不能在同一个进程身份下冒充另一个 agent。

---

## 能跑

运维层。全部通过只说明安装、进程身份、五个 tool、宿主加载没坏。

### 1. `doctor` 退出码 0

```bash
uv run compound-memory doctor
echo "doctor_exit=$?"
```

stdout 是 JSON（`ok`、`problems`、`root`、`attestation`、`retrieval`、`write_lock`、`git`）。告警在 stderr。退出码 0 时 stderr 有一行 `doctor: ok`。

退出码 1 的硬失败：`COMPOUND_MEMORY_AGENT_ID` 未设置或空白、根目录缺失或不是目录、写锁加不上、记忆库不是可用 git 仓库（git 不在 PATH、没有 `.git`、或 `git status` 失败）。`retrieval.mode=lexical` 和脏工作树只报告，不因此失败。`doctor` 不创建缺失根目录，也不对非仓库 `git init`。

### 2. 五个 MCP tool 各走一次

宿主里让 agent 各调一次，顺序固定，这样后一步用得到前一步的 id。`memory_search` 与 `memory_write` 至少各一次；`memory_get`、`memory_link`、`memory_feedback` 也要各一次，五个缺一则这节不算过。

| MCP tool | 通过时看到 | CLI 对照（同一库、同一 `COMPOUND_MEMORY_AGENT_ID`） |
|---|---|---|
| `memory_write` | 返回里有 `id`，且没有把正文丢掉 | `write` |
| `memory_search` | `{"hits": [...], "count", "retrieval"}`，`hits` 里能找到刚写入的正文 | `search`（裸数组，没有 `hits` 包装） |
| `memory_get` | 对刚写入的 id 返回 `found: true` | `get` |
| `memory_link` | 两条同 ns 记忆链上之后，再 `memory_get` 能看到邻居 | `link` |
| `memory_feedback` | `found: true`，`uses` 比反馈前大 1 | `feedback` |

CLI 冒烟（空库、`agent-claude`）：

```bash
uv run compound-memory write \
  "Eval smoke: keep serverless handlers under the 10s platform timeout." \
  fact agent-claude --key eval-smoke-timeout
uv run compound-memory write \
  "Eval smoke: prefer a short table over a long paragraph." \
  fact agent-claude --key eval-smoke-style
uv run compound-memory search "serverless timeout" --explain
uv run compound-memory get <id-timeout>
uv run compound-memory link <id-timeout> <id-style> --agent agent-claude
uv run compound-memory feedback <id-timeout> agent-claude --outcome success
uv run compound-memory get <id-timeout>
```

`<id-timeout>` / `<id-style>` 用两次 `write` 返回的 `id`。`search` 默认 `top_k=5`，空库两条足够。`--explain` 只在命中上附加排序分量，不是另一条命令。

`memory_search` 的通过标准是宿主返回带 `hits` 的对象。CLI `search` 返回裸数组，这是既有差异，不是冒烟失败。

### 3. 宿主安装冒烟

只核对「工具出现了、身份是这个宿主」。不在这里判有效。

| 宿主 | 安装与核对 |
|---|---|
| Claude Code | [`integrations/claude/README.md`](../../integrations/claude/README.md)：`claude plugin marketplace add` 然后 `claude plugin install compound-memory@compound-memory`。默认 `COMPOUND_MEMORY_AGENT_ID=agent-claude`。新会话里应能调到五个 tool；`memory_search` 返回 `{"hits": [...]}`。SessionStart 提醒是否出现，按该 README 的 `/hooks` 步骤看 |
| WorkBuddy / CodeBuddy | [`integrations/workbuddy/README.md`](../../integrations/workbuddy/README.md)：`/plugin marketplace add chinwe/compound-memory` 然后 `/plugin install compound-memory@compound-memory`。默认 `agent-workbuddy`。同样用一次 `memory_search` 看 `hits` |
| DeepSeek Harness (dsh) | 尚无 `integrations/` 插件，状态以 [`docs/agent-integration.md`](../agent-integration.md) §5 为准。配置写在 `~/.dsh/profiles/web/cordis.patch.yml`，`COMPOUND_MEMORY_AGENT_ID=agent-deepseek`。重启后跑 `dsh web --dump-config \| grep -A3 mcp`，并确认五个 tool（`memory_write` / `memory_search` / `memory_get` / `memory_link` / `memory_feedback`）已注册 |

---

## 有效

任务层。同一道题跑两遍，人对照记录。`uses` 上升或只回了 `success`，不算有效。

### A/B 协议

固定题在 [`benchmarks/must-hit.yaml`](benchmarks/must-hit.yaml)。每题两次，题面（`prompt`）一字不改，只改记忆库。

1. **A，空记忆。** 新的 `COMPOUND_MEMORY_ROOT`，`init` 之后不 `write`。agent 做 `prompt`。记下它有没有踩 `pass_a` 里写的坑。
2. **B，有记忆。** 另一个新根。只种这一题的 `seed`（命令见 [`benchmarks/README.md`](benchmarks/README.md)）。agent 必须先 `memory_search`（或 CLI `search`），采纳或驳回之后 `memory_feedback`，`outcome` 用该题要求的值。
3. 两次用同一个宿主、同一份使用规范（[`skills/compound-memory/SKILL.md`](../../skills/compound-memory/SKILL.md)）。不要在 B 的题面里多写答案。
4. 每遍抄一份 [`results/TEMPLATE.md`](results/TEMPLATE.md)。A 与 B 各一份。
5. 一套完整记录至少包含：一条偏好或约定题的 A/B，[`cross-agent-test-clock`](benchmarks/must-hit.yaml) 的跨宿主读写，以及两条负向题 `negative-timeout-60s`（`failure`）和 `negative-poetry`（`contradiction`）。只交 `success` 的记录不完整。

B 里「先检索再反馈」是归因条件。方案碰巧写对、但没有 `search`，这题的 B 不算过。

### 跨宿主读写

`cross-agent-test-clock` 的种入和读取要用两个进程身份。读取方搜索 `_shared` 不需要 `--reader`。

```bash
export COMPOUND_MEMORY_ROOT="${TMPDIR:-/tmp}/compound-memory-eval-cross"
COMPOUND_MEMORY_AGENT_ID=agent-claude uv run compound-memory init
COMPOUND_MEMORY_AGENT_ID=agent-claude uv run compound-memory write \
  "测试断言日期用 conftest 的 CLOCK_DATE，不要贴真实墙钟。" \
  fact agent-claude --key test-clock
COMPOUND_MEMORY_AGENT_ID=agent-workbuddy uv run compound-memory search "CLOCK_DATE"
COMPOUND_MEMORY_AGENT_ID=agent-workbuddy uv run compound-memory feedback <id> agent-workbuddy --outcome success
COMPOUND_MEMORY_AGENT_ID=agent-workbuddy uv run compound-memory explain <id>
```

`explain` 是 CLI，不是第六个 MCP tool。通过时 `found: true`，`validated_by` 含 `agent-workbuddy`，`evidence.success_count` 至少 1。

`stats` 的 `cross_validated` 计的是 `validated_by` 去重后至少两个 agent 的记忆条数。上面只有一个验证宿主，计数保持 0。这不否定「第二个宿主读到了」。要让计数 +1，需要第二个不同的 agent 再对同一条回一次 `success`（例如 `agent-zcode`）。一次跨宿主 `success` 会给置信度加一次 0.15，并写入 `validated_by`，但不会单独把 `cross_validated` 抬起来。

### 负向路径必须记下来

`failure` 与 `contradiction` 都会让 `uses` 和 `last_used` 前进。所以 `uses` 变大不能区分「记忆帮了忙」和「记忆帮了倒忙」。

错事实被采纳之后，反馈用：

```bash
uv run compound-memory feedback <id> agent-claude --outcome failure
uv run compound-memory explain <id>
```

`explain` 里 `evidence.failure_count` 至少 1。未冻结时置信度按 −0.2 折算（地板 0.05）。新写入从 0.5 起，单次 `failure` 后是 0.3。

驳回一条记忆：

```bash
uv run compound-memory feedback <id> agent-claude --outcome contradiction
uv run compound-memory explain <id>
uv run compound-memory review-queue
uv run compound-memory git-log --limit 20 --grep 'outcome=contradiction'
```

`contradiction` 冻结置信度（仍是 0.5，不升），`evidence.contradiction_count` 至少 1。CLI `review-queue` 的 JSON 里应有一条 `<date> contradiction <id>: by <agent> (`（落盘文件 `review-queue.md` 同一行多一个 `- ` 前缀）。`git-log` 的过滤发生在 `--limit` 取数之后，默认只看 5 条，核对负向提交时把 `--limit` 加大。提交消息形如 `feedback <id> by <agent>: outcome=<o> uses=<n> conf=<x>`。

`failure` 同样可以用 `git-log --limit 20 --grep 'outcome=failure'` 核对。裁决队列用既有的 `review-resolve`，本页不要求跑完裁决才算「记下来了」。

### `stats` 是健康度，不是证明

```bash
uv run compound-memory stats
uv run python scripts/eval_stats_snapshot.py --root "$COMPOUND_MEMORY_ROOT"
```

脚本只调用 CLI `stats`，把 stdout 的 JSON 写到 `docs/eval/results/<UTC 时间>-stats.json`，并写一份同名 `.md` 笔记（命令、退出码、stderr）。退出码与 `stats` 相同。它不读记忆正文，也不打分。

| 字段 | 实际含义 | 不能单独拿来声称有效 |
|---|---|---|
| `uses`、`uses_histogram` | 反馈次数。每种 `outcome` 都 +1 | 上升只说明有人回了反馈，含 `failure` / `contradiction` |
| `recent_feedback_7d` | `last_used` 落在最近 7 天的条数 | 活性窗口，不是做对的题数 |
| `cross_validated` | `validated_by` 去重后 ≥2 的条数 | 不是「有过一次跨宿主反馈」。宿主之间也可以共享同一个错假设，计数上升不等于语义正确 |
| `avg_confidence`、`confidence_histogram` | 证据折算后的分布 | 只回 `success` 会把置信度抬高，即使任务并没有少踩坑 |
| `retrieval`、`attestation`、`expired_active` | 词面还是向量、进程身份、`valid_until` 已过仍留在活动区 | 安装与库状态 |

单条记忆的证据用 `explain <id>`：`evidence.success_count` / `failure_count` / `contradiction_count`、`validated_by`、`confidence`。

### 对外说法：收口标准

没有现成的比率。下表是以后可以引用的收口，每一格都要能指回记录或命令输出。不要填没有跑过的数字。

| 信号 | 记下什么才算数 | 不够 |
|---|---|---|
| 闭环（同一题 A 踩了、B 避开） | 该题 A/B 两份 TEMPLATE：`repeat_pitfalls` 在 A 为踩中、在 B 为避开；B 的 `steps_to_success` 写明 `search` 然后 `feedback` | 单次演示；B 没有检索却碰巧写对；只看 `uses` |
| `cross_validated` 增长 | 窗口前后各一份 `stats` JSON，计数上升；并用 `explain` 核对增长来自 `validated_by` 里至少两个不同 agent 的 `success` | 一次跨宿主读取（计数可以仍是 0）；把计数上升说成「已证明正确」 |
| `failure` 被记下来 | TEMPLATE 的 `feedback_outcome` 为 `failure` 或 `contradiction`；`explain` 的对应计数 ≥1；负向题 `negative-timeout-60s` 与 `negative-poetry` 各有一份 B | 全是 `success`；`uses` 上升但 `failure_count` 仍是 0 |

三条一起出现，才够把一次核对说成「这套题上，记忆改变了做法，并且错记忆没有被记成成功」。缺任何一条，对外只说「能跑」或「记录不完整」。
