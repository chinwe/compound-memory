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

灌库不走 `store.write()`：逐条 `_sync_indexes` 在 tokens.json 全量重写下是 O(n²)，
2.4 万条会话不可行。`ingest.py` 直接 `_save` 落盘 + 一次性 `rebuild_index()`
（experiments 旁路脚本允许使用内部路径；生产 API 不受影响）。

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

## 阶段 B（端到端 QA，未跑）

标准 LongMemEval 报的是端到端 QA 准确率（LLM 生成 + LLM judge），需要 OpenAI 兼容
API key。本机无任何 LLM API key（2026-10-04 实测 env 为空），故先交付阶段 A。
补跑方式：新增 `qa_eval.py`（search top-k → 拼 context → 生成 → judge），env 提供
`OPENAI_BASE_URL` / `OPENAI_API_KEY` / `LME_MODEL` 即可。注意：端到端数字依赖
生成模型与 judge 的选择，与各厂商自报数字不可直接互比（见竞品分析 20261004_513d10）。
