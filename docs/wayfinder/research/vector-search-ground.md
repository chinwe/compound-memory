# 研究笔记：向量检索地基（sqlite-vec 与本地 embedding 的现实可行性）

- Ticket: [002-vector-search-ground](../tickets/002-vector-search-ground.md)
- 分支: `research/vector-search-ground`
- 日期: 2026-10-01
- 方法: 一手来源核验（官方文档 / PyPI JSON API / HF 模型库 API / 论文原文）+ 本机实测（macOS 12 x64，darwin 21.6；项目 `.venv` Python 3.12.13；uv 以 `UV_ONLY_BINARY=':all:'` 安装，只验证 wheel 路线）

## 0. 结论速览

| 档位 | 事实指向 |
|---|---|
| 做 | sqlite-vec 0.1.9 在本机全链路可用（wheel-only 安装、扩展加载、千条级性能）零阻塞；embedding 有轻依赖路线（onnxruntime 1.19.2 16MB wheel + BGE-small-zh int8 23MB 模型）；若做，证据最足的形态是**加一路向量召回再与词面路融合**，不是替换 BM25 |
| 缓做 | "短文本、中英混合、千条级"细分场景**没有找到公开实测数据**支持向量增益；千条级暴力余弦本来就在毫秒级，性能不构成理由；依赖钉版本（onnxruntime ≤1.19.2）是持续成本；增益需自建评测集验证后再投入 |
| 不做 | torch / sentence-transformers 路线在本机 macOS x64 已无新版官方 wheel（torch 2.2.2 止步，2.4.1 起仅 arm64）；llama-cpp-python PyPI 仅 sdist 无 wheel，本地编译违背本仓 wheel-only 约束——这两条集成路径事实性排除 |

## 1. sqlite-vec 集成现实

### 包与安装

- 官方 Python 包名 **`sqlite-vec`**（PyPI），当前版本 **0.1.9**，MIT / Apache-2.0 双许可。来源: [PyPI](https://pypi.org/project/sqlite-vec/)、[官方 Python 文档](https://alexgarcia.xyz/sqlite-vec/python.html)
- **不是纯 Python 包，也无需本地编译**：每个平台一个预编译 wheel（包内是平台专用 SQLite 扩展二进制 + 加载辅助）。macOS x64 对应 `sqlite_vec-0.1.9-py3-none-macosx_10_6_x86_64.whl`（128 KB，标签 macOS 10.6+，覆盖本机 macOS 12）。来源: PyPI JSON API 文件清单
- **本机实测**（2026-10-01，`UV_ONLY_BINARY=':all:' uv pip install sqlite-vec`）：安装成功（网络间歇超时，重试即过）；`sqlite_vec.load(db)` 加载成功，`vec_version()=v0.1.9`。项目 `.venv` 的 Python 3.12.13 自带 SQLite 3.50.4，`enable_load_extension` 存在且可用
- 与 stdlib sqlite3 的关系：完全走标准 `load_extension` 通道——`db.enable_load_extension(True); sqlite_vec.load(db); db.enable_load_extension(False)`，之后 `vec0` 虚拟表、`vec_distance_cosine` 等 SQL 即可用。辅助函数 `serialize_float32()`（struct.pack 包装）把 list[float] 转 BLOB；NumPy float32 数组可凭 buffer protocol 直传。来源: [官方 Python 文档](https://alexgarcia.xyz/sqlite-vec/python.html)

### 已知坑

1. **macOS 系统 Python 陷阱**（官方文档明确警告）：macOS 自带 SQLite 编译时关闭了扩展加载，系统 Python 会报 `AttributeError: 'sqlite3.Connection' object has no attribute 'enable_load_extension'`；官方建议改用 Homebrew Python。本项目 uv venv 用的 Python **已实测不受影响**。来源: [官方 Python 文档 macOS 节](https://alexgarcia.xyz/sqlite-vec/python.html)
2. **SQLite 版本**：官方推荐 ≥3.41（非强制，旧版部分查询行为不对）。Python 3.12 的构建普遍自带新版 SQLite（本机 3.50.4 满足）。来源同上
3. **vec0 目前只做暴力扫描（brute force），没有 ANN 索引**；ANN 在 roadmap（issue #25）。官方 v0.1.0 基准：sift1M（100 万 × 128 维，k=20）查询 17ms——百万级都可行，千条级完全不构成问题。来源: [issue #25](https://github.com/asg017/sqlite-vec/issues/25)、[v0.1.0 发布博文](https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html)
4. **版本成熟度**：仍在 0.1.x，SQL API 可能变动；项目按"索引可重建的缓存"引入则风险可控（与本仓"索引即缓存"不变量天然契合）
5. 本机性能冒烟（`:memory:`，1000 条 × 384 维 float32）：批量 insert 20ms，top-5 KNN 查询平均 **0.74ms**

## 2. 本地 embedding 候选对比

模型体积来自 HuggingFace 库 API（HF 直连在本网络超时，经 hf-mirror.com 核验原库文件清单）。

| 候选 | 模型体积 | 运行依赖 | 本机 wheel 可得性 | 许可 | 备注 |
|---|---|---|---|---|---|
| **BGE-small-zh-v1.5 (BAAI)** | 原版 91.4MB；[Xenova ONNX int8](https://hf-mirror.com/Xenova/bge-small-zh-v1.5) **22.9MB** | transformers+torch（重）或 onnxruntime+tokenizers（轻） | ONNX 路线可行（见下） | MIT（模型可免费商用） | 24M 参数，512 维，中文特化；C-MTEB Retrieval 61.77；月下载约 480 万 |
| **paraphrase-multilingual-MiniLM-L12-v2** | 原版 448.8MB；Xenova ONNX int8 **112.8MB** | 同上 | 同上 | Apache-2.0 | 50+ 语言真多语言，中英混合/跨语言查询更稳，但检索分数低于 BGE 中文（C-MTEB Retrieval 59.95，multilingual-e5-small 口径） |
| **sentence-transformers 6.1.0**（加载框架，非模型） | — | torch≥2.2 + transformers 5 + scikit-learn + scipy | **torch macOS x86_64 wheel 止于 2.2.2**（2.4.1 起仅 arm64，PyPI JSON 核验）；需钉 torch 2.2.2（143MB）且与新版库兼容性自担 | Apache-2.0 | 依赖最重的路线 |
| **llama.cpp embedding** | GGUF embedding 模型自选（如 Qwen3-Embedding-0.6B Q8 数百 MB 级） | [llama-cpp-python PyPI 仅 sdist 无 wheel](https://pypi.org/pypi/llama-cpp-python/json)（0.3.36 实测核验），默认 pip 安装即本地编译 C++ → **违背 wheel-only 约束** | 例外：官方 [GitHub releases](https://github.com/ggml-org/llama.cpp/releases) 有 macOS 预编译二进制，`llama-server --embeddings` 可在 127.0.0.1 起 OpenAI 兼容服务（进程外、本地回环，不破坏本地优先） | MIT | 引入常驻进程与 GGUF 模型管理，对本仓形态偏重 |

### 依赖边界事实（PyPI JSON API 逐版本核验）

- **onnxruntime**：本机（macOS 12 x64）可装的最新版是 **1.19.2**（`macosx_11_0_universal2`，16MB）；1.20.1/1.22.0 为 `macosx_13_0_universal2`（需 macOS 13，pip 标签解析会拒）；1.23.2 的 x86_64 wheel 也是 `macosx_13_0`；1.25.0 起仅 arm64。即 macOS x64 必须钉 `onnxruntime<=1.19.2`
- **torch**：x86_64 macOS wheel 止于 **2.2.2**（`macosx_10_9_x86_64`，143MB）；2.4.1 起仅 arm64
- **sqlite-vec**：`macosx_10_6_x86_64`，本机无障碍

### 本机 CPU 延迟实测（BGE-small-zh-v1.5 ONNX int8 + onnxruntime 1.19.2 CPU）

- 单条短文本（约 30 字）：**8.3 ms**
- 1000 条全量编码（全库重建）：**8.3 s**
- 1000×512 维 numpy 暴力余弦 top-3：12.5 ms（sqlite-vec vec0 实测 0.74ms，更快）
- 量级结论：千条库"写入时编码 + 查询时双路召回"的延迟完全可接受；8.3ms/条也意味着写入路径同步编码无压力

### 离线可用性

- 上述模型下载后本地推理，全程无外发，符合 spec story 17 本地优先；HF 下载在本网络需走 hf-mirror 镜像（HF 主站直连超时，实测）
- BGE 检索用法注意（模型卡 FAQ）：短查询建议加 instruction"为这个句子生成表示以用于检索相关文章："（v1.5 不加只轻微降级）；**相似度绝对值集中在 [0.6,1] 区间**，不可与 BM25 分数直接比大小——融合必须走 rank 系（如 RRF）或先归一

## 3. 增益证据与倾向

### 支持向量路的证据

- **C-MTEB**（[C-Pack 论文](https://arxiv.org/abs/2309.07597)，中文 31/35 数据集基准）：BGE 系在中文检索任务大幅领先旧 dense 模型（bge-small-zh-v1.5 Retrieval 61.77 vs text2vec-base 38.79）。来源: [BAAI 模型卡评测表](https://hf-mirror.com/BAAI/bge-small-zh-v1.5)
- 语义改写查询（同义不同词）是词面法的结构性死穴，向量路天然覆盖——这是定性共识，也是记忆场景最可能的增益来源（查询是自然语言任务描述，与记忆条目措辞往往不同）

### 不支持盲目替换 BM25 的证据

- **BEIR**（[Thakur et al. 2021](https://arxiv.org/html/2104.08663v4)，英文 18 数据集零样本）：原文结论 "BM25 is a robust baseline"、"Overall, BM25 remains a strong baseline for zero-shot text retrieval"；dense 模型（ANCE、TAS-B 级）在分布外数据上经常不及 BM25；"no single approach consistently outperforms"。即：向量不是无条件赢
- **场景差距（本研究的核心发现）**：公开 benchmark 与本仓场景错位——C-MTEB/T2Retrieval 是百万级语料、长文档段落检索；本仓是**千条级、短文本、查询即任务描述**。千条级下两路的排序差异被显著压缩，且未找到任何"中文短文本千条级 BM25 vs 向量"的公开实测。**结论：增益必须靠自建评测集（真实记忆条目 + 真实查询）实测，不能靠 benchmark 外推**
- 本仓现有基线是 BM25 + **CJK 单字 bigram**——中文词面匹配已绕过无分词问题，比裸 BM25 中文基线强，进一步压缩向量路的边际增益

### 融合形态的证据

- **RRF**（[Cormack et al., SIGIR 2009](https://dl.acm.org/doi/10.1145/1571941.1572114)）："RRF almost invariably improved on the best of the combined results"——多路 rank 融合几乎总优于最好单路。映射到本仓：若做，最小形态是 `scoring.rank` 增加一路向量相似（0.45 权重槽或 RRF），与既有词面路并存
- BGE 相似度分布集中（0.6-1）的事实强化了"rank 融合优于分数线性加权"的判断

## 4. 对 compound-memory 的落地含义（供取舍票参考）

1. `Index` 的"活动必被索引、归档必不在索引、缓存可重建"不变量对向量索引同样适用：vec0 表 + 编码缓存全部可由 MD 文件重建，坏了大不了重算（千条 8.3s）
2. embedding 写入时机：MemoryStore 写路径同步编码（8.3ms/条）即可，无需批量任务；归档/复活时随活性迁移
3. 中英混合建议选多语言模型（MiniLM multilingual int8 113MB）或接受 BGE-small-zh 对英文条目的降级——这本身是取舍票要拍的问题，两条路 wheel 可得性相同
4. 依赖钉版：`onnxruntime<=1.19.2`（macOS 13+ 环境才能用新版），需在 pyproject 显式标注原因

## 来源

- sqlite-vec 官方 Python 文档: https://alexgarcia.xyz/sqlite-vec/python.html
- sqlite-vec PyPI（版本/许可/wheel 清单经 JSON API 核验）: https://pypi.org/project/sqlite-vec/
- sqlite-vec 仓库与 ANN roadmap: https://github.com/asg017/sqlite-vec 、https://github.com/asg017/sqlite-vec/issues/25
- sqlite-vec v0.1.0 发布博文（brute-force 声明与 sift1M 基准）: https://alexgarcia.xyz/blog/2024/sqlite-vec-stable-release/index.html
- BAAI/bge-small-zh-v1.5 模型卡（C-MTEB 分数、MIT、相似度分布 FAQ、instruction）: https://hf-mirror.com/BAAI/bge-small-zh-v1.5
- Xenova/bge-small-zh-v1.5 ONNX 量化版: https://hf-mirror.com/Xenova/bge-small-zh-v1.5
- sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2: https://hf-mirror.com/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
- Xenova/paraphrase-multilingual-MiniLM-L12-v2 ONNX: https://hf-mirror.com/Xenova/paraphrase-multilingual-MiniLM-L12-v2
- PyPI JSON API（onnxruntime 1.19.2/1.20.1/1.22.0/1.23.2/1.24.0/1.25.0、torch 2.2.2/2.4.1/2.5.1+、llama-cpp-python 0.3.36、sentence-transformers 6.1.0 的平台 wheel 逐版本核验）: https://pypi.org/pypi/{onnxruntime,torch,llama-cpp-python,sentence-transformers}/json
- BEIR 论文: https://arxiv.org/html/2104.08663v4
- C-Pack / C-MTEB 论文: https://arxiv.org/abs/2309.07597
- Cormack et al. RRF (SIGIR 2009): https://dl.acm.org/doi/10.1145/1571941.1572114
- llama-cpp-python 文档（embedding API、安装需编译）: https://llama-cpp-python.readthedocs.io/
- llama.cpp 预编译发布: https://github.com/ggml-org/llama.cpp/releases
- 本机实测（2026-10-01，命令与原始数字见本文各节）：sqlite-vec 安装/加载/KNN 性能；onnxruntime 1.19.2 安装；BGE-small ONNX int8 编码与余弦延迟
