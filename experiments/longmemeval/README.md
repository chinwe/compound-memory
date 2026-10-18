# LongMemEval 检索评测（阶段 A：零 LLM）

在公开长程对话记忆基准 [LongMemEval](https://arxiv.org/abs/2410.10813) 上测
compound-memory 的**检索层**质量。用 cleaned 版数据集（`xiaowu0162/longmemeval-cleaned`，
移除了原版干扰答案的噪声会话）。

评测口径：**零 LLM**——只测「问题原文能否从该题的 haystack 会话池中召回官方标注的
证据会话（answer_session_ids）」，指标 recall@5/10/20 与 MRR@20。不做生成、不做 LLM
判分，因此数字完全确定性、可复现，且不依赖任何 API key。

## 协议

- 一个 session = 一条 episode 记忆（User/Assistant 轮次拼接），`created` 取 session 真实日期。
- **每题独占一个私有 ns**（`agent-lme-<question_id>`），与官方 per-question 协议对齐
  （干扰只来自题内 haystack，不引入跨题噪声）。
- query = question 原文，`top_k=20`，一次检索切 top-5/10/20。
- 分 question_type 输出 breakdown：`temporal-reasoning` / `knowledge-update` 是
  「新旧事实并存」模式的试金石（2026-10-04 新增的 valid_from/valid_until 时态标注
  在此评测中**未启用**——会话没有手工标注有效期，是纯粹的检索层基线）。

## 数据下载（约 280MB，走 HF 镜像，落 `data/`，已 gitignore）

```bash
curl -L -o experiments/longmemeval/data/longmemeval_s_cleaned.json \
  "https://hf-mirror.com/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"
```

## 复现

```bash
# 词面（BM25）全量
uv run python experiments/longmemeval/ingest.py --root experiments/longmemeval/data/stores-lex-500
uv run python experiments/longmemeval/eval_retrieval.py \
  --root experiments/longmemeval/data/stores-lex-500 \
  --mapping experiments/longmemeval/runs/mapping-500q.json

# 向量路 pilot（前 50 题，需 vec extra + 本地 BGE 模型；编码约 15 分钟）
uv run python experiments/longmemeval/ingest.py --root experiments/longmemeval/data/stores-vec-50 --limit 50 --vector
uv run python experiments/longmemeval/eval_retrieval.py \
  --root experiments/longmemeval/data/stores-vec-50 \
  --mapping experiments/longmemeval/runs/mapping-vec-50q.json --limit 50 --vector
```

## 性能注记

灌库走 `store.batch()`（`experiments/longmemeval/ingest.py`）：批内逐条 `write`
校验语义与单条相同，词法索引落盘和（`--vector` 时）向量编码收拢到批尾一次。
逐条 `_sync_indexes` 在 tokens.json 全量重写下是 O(n²)，2.4 万条会话不可行，
所以脚本不再绕过 `_save`，也不再在批尾另跑 `rebuild_index()`。每题的私有 ns
以该 ns 属主身份写入（`source` = `agent-lme-<question_id>`）。

复现命令见上一节：词面全量用 `--root experiments/longmemeval/data/stores-lex-500`；
向量 pilot 加 `--limit 50 --vector`（需 vec extra 与本地 BGE 模型）。mapping
默认写到 `experiments/longmemeval/runs/`，可用 `--mapping-out` 改路径。

## 结果

**阶段 A 词面全量（500 题 / 23,867 会话，纯 BM25，`runs/retrieval-lex-500q.json`）**
与 **向量路全量（RRF 融合，分块编码 101 分钟，`runs/retrieval-vec-500q.json`）**：

| 指标 | 词面 | 向量（RRF） | 差值 |
|---|---|---|---|
| recall@5 | 0.966 | 0.968 | +0.002 |
| recall@10 | 0.980 | 0.982 | +0.002 |
| recall@20 | 0.994 | 0.996 | +0.002 |
| MRR@20 | 0.889 | 0.891 | +0.002 |

分题型（向量 vs 词面的 MRR@20）：knowledge-update 0.958 vs 0.950、multi-session
0.894 vs 0.881、single-session-user 0.883 vs 0.888、temporal-reasoning 0.878 vs 0.890、
single-session-preference 0.570 vs 0.550（recall@5 0.867 vs 0.833，recall@20 1.000 vs 0.967）。

- **向量路在 LongMemEval 上增益很小**（overall +0.002）：问题与证据会话的词面
  重叠天然高，BM25 已近天花板（recall@20 0.994），向量只剩尾部 tie-break 价值。
  唯一实质受益是 preference 题（recall@5 +3.3pp、recall@20 补到满）。
  与 vec-spike 内部审计（中文短查询 MRR 0.79→0.95）不矛盾：中文真实记忆场景的
  词面盲区远大于英文 QA 数据集——向量路的价值随「查询-文档词面重叠度」下降而上升。
- 最弱项仍是 `single-session-preference`（MRR 0.570）：证据在 top-20 内但排位靠后，
  与 recency-audit 的马太效应同型。
- `knowledge-update` 检索层满血（1.000）：新旧并存时证据必然命中——瓶颈在生成段的
  「用哪一版」，即阶段 B 与 valid_until 时态标注的用武之地。
- 500 查询检索 252s（词面）/ 228s（向量）。灌库暴露并顺手修了一个规模化瓶颈：
  `_candidates` 的 ns 过滤原来发生在 parse 之后，常见词命中近全库时浪费巨大；
  已改为 rel-path ns 前缀在 parse 前剪枝（`storage.py`，语义不变）。
- 编码耗时实测：全库 23,867 条分块编码 6,056s（~253ms/条，512-token 长会话
  padding 主导；巨批单 run 形态下同任务不可完成——issue #16 修复的前提）。

## Windows 11 复测（2026-10-04）

同协议同数据全量复现（500 题 / 23,867 会话），环境与代码基线同
`experiments/perf-bench/README.md` 的 Windows 复测小节（当前 HEAD，
含 Windows rel_path POSIX 归一修复）。产物：`runs/retrieval-lex-500q-win.json`
与 `runs/retrieval-vec-500q-win.json`（mapping 同后缀，`ingest.py` 新增
`--mapping-out` 避免覆盖既有产物）。

| 指标 | 词面 | 向量（RRF） | 差值 |
|---|---|---|---|
| recall@5 | 0.968 | 0.968 | 0 |
| recall@10 | 0.982 | 0.982 | 0 |
| recall@20 | 0.996 | 0.996 | 0 |
| MRR@20 | 0.8919 | 0.8926 | +0.0007 |

- 与 macOS 结果逐位级一致（各指标差异 ≤0.004，可归因 recency 先验的
  真实日期差——两机跑的日期不同，个别 tie-break 排位微移）；「向量增益
  微小」的结论跨平台复现：knowledge-update MRR 0.939→0.965（macOS
  0.950→0.958 同向），preference 仍最弱（0.575）。
- 时长：词面检索 87.6s（macOS 252s，~2.9x）；**向量检索 238.5s（macOS
  228s，持平）**——词面路随 CPU 快，向量路瓶颈在 sqlite-vec KNN 的
  C 层全表扫描，平台差距被摊薄；向量灌库 rebuild 1,697s（~71ms/条，
  macOS 6,056s 的 ~3.6x——长会话 512-token padding 主导，短文本场景
  的 6x 差距在此收敛）。

## 2026-10-18 回归（词面，当前 main）

10-04 基线之后检索路径经过 `is_retrievable`（#67）、`active_neighbors` 收口、rank/explain 与 project scope。本轮只复测**检索层**（阶段 A，零 LLM）：问题原文能否召回 `answer_session_ids`。不把这些数字当成产品效果。向量路未跑。阶段 B 仍未跑。

新产物都带 `-20261018` 后缀（`runs/retrieval-lex-50q-20261018.json`、`runs/retrieval-lex-500q-20261018.json` 及对应 mapping）。`retrieval-lex-500q.json`、`retrieval-lex-50q.json` 与 `-win` 变体未改。

冒烟是前 50 题（全是 `single-session-user`），对照归档 `runs/retrieval-lex-50q.json`。recall@20 持平，MRR@20 下降 0.021，超过 0.01 门禁，因此继续跑了词面全量 500。

| 指标 | 2026-10-04（50 题） | 2026-10-18 冒烟 | Δ |
|---|---|---|---|
| recall@5 | 1.000 | 1.000 | 0 |
| recall@10 | 1.000 | 1.000 | 0 |
| recall@20 | 1.000 | 1.000 | 0 |
| MRR@20 | 0.914 | 0.893 | −0.021 |

这 0.021 落在 BM25 饱和后的并列上：冒烟里挪动名次的题，top 命中都是 similarity=1.0、score=0.85（episode 的 τ=30 天，会话日期已把 recency 压到 0.5 底座；把 clock 钉在 2026-10-04 仍然是 0.893）。分数打到四位小数后完全相同，名次由候选路径序决定，路径里含随机 id。同一次代码再灌这 50 题，MRR 是 0.9057。50 题 MRR 单独不能当回归结论。

全量 500（`runs/retrieval-lex-500q-20261018.json`；检索 29s，灌库 1690s）对照归档 `runs/retrieval-lex-500q.json`：

| 指标 | 2026-10-04 | 2026-10-18 | Δ |
|---|---|---|---|
| recall@5 | 0.966 | 0.968 | +0.002 |
| recall@10 | 0.980 | 0.982 | +0.002 |
| recall@20 | 0.994 | 0.996 | +0.002 |
| MRR@20 | 0.8886 | 0.8907 | +0.0021 |

**检索层没有退步。** 四项相对 10-04 归档都没有下降；变化幅度约 0.002，小于 0.01 门禁。同日重跑基线提交 `33525c9`（同一数据集、同一脚本协议）得到的 recall@5/10/20 与归档逐位一致，MRR@20 为 0.8907，与本次全量相同。

掉出 top-k 的题（对照这次 `33525c9` 重跑的逐题名次；归档 JSON 没有逐题 id）：

| 门槛 | 掉出的 question_id |
|---|---|
| top-5 | （无） |
| top-10 | （无） |
| top-20 | （无） |

映射层面多出来的一次 top-20 命中是 `982b5123_abs`（temporal-reasoning，本次第 1）。重跑里该文件正文已经是证据会话、排在第 1，但 mem id `20261018_9de66b` 同时写进了另一个 ns，mapping 留下了另一个 session id，评分脚本把它记成未命中。6 位 hex 的 id 碰撞是这条协议的既有噪声（归档 mapping 23843 个键 / 23867 个 session，本次 23854 个键）。其余 24 处名次变化都在 score=0.85 的并列组内部，分数不变。

分题型相对归档 JSON 的 MRR 会摆动（temporal-reasoning 0.8897→0.8709）。同日重跑旧代码时该题型 MRR 已是 0.8659，本次 0.8709 高于这次重跑。preference 的 recall@20 在归档里是 0.9667，重跑与本次都是 1.000。

## 重测（2026-10-18，复用旧库与向量路，编码漂移根因）

与上一节（#95，重灌库的词面复测）互补：本节在当前 HEAD **复用 2026-10-04 灌好的库
与 mapping** 重跑两路（产物 `runs/retrieval-*-500q-recheck*.json`，不覆盖基线）——
重灌库引入 tie-break 抖动（#95 实测 ±0.002），复用旧库则应逐位复现，两条路径互证。

- **词面路：28 项指标（overall + 6 题型 × 4 指标）与基线逐位一致**——词面检索语义
  无回归。耗时 284.7s vs 基线 252s，属沙箱负载波动（非回归信号）。
- **向量路：recall@10 / recall@20 与基线逐位一致**；recall@5 0.968→0.966、
  MRR@20 0.8906→0.8670（各题型 -0.014~-0.032）。
- 向量路差异的根因**不是检索代码**：融合公式、RRF_K/PRIOR_EPSILON/mix 常量、
  `vec_rank` 构造逐字符一致，`is_retrievable` 过滤对评测库（无归档/无过期/无
  project）语义等价，uv.lock 漂移经查仅为镜像 URL 重写（版本与 hash 全同）。
  真因是 **vectors.db 已被清空，重测走对账重建的重编码**：onnx BGE 编码对
  batch 内 padding 敏感——同文本单独编码 vs 混入长文本编码，实测
  max|Δ|=1.0e-2、cosine=0.9962（同 batch 重复编码逐位一致，确定性无问题）。
  基线向量产生于 ingest 灌库分块、重测向量产生于对账重建分块，块内 padding
  不同 → 向量数值漂移 → KNN 密集区排序微移 → MRR 小幅移动；召回池宽
  （k=64）故 recall@10/20 不动。
- 同一 vectors.db 二跑全指标 0 差异（当前 HEAD 检索确定性成立）。
- **工程含义：向量路跨 run 对比必须固定 vectors.db**——缓存重建（ingest 或
  对账）会因分块组织不同引入 ~±0.02 MRR 级漂移，这是编码实现的数值性质，
  不是检索逻辑的变化。

## 阶段 B（端到端 QA，未跑）

标准 LongMemEval 报的是端到端 QA 准确率（LLM 生成 + LLM judge），需要 OpenAI 兼容
API key。本机无任何 LLM API key（2026-10-04 实测 env 为空），故先交付阶段 A。
补跑方式：新增 `qa_eval.py`（search top-k → 拼 context → 生成 → judge），env 提供
`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `LME_MODEL` 即可。注意：端到端数字依赖
生成模型与 judge 的选择，与各厂商自报数字不可直接互比（见竞品分析 20261004_513d10）。
