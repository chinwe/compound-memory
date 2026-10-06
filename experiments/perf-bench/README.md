# perf-bench：关键路径分规模延迟基准

- 日期：2026-10-03（基线随 commit 记录）
- 背景：spec 0001「索引即缓存」的读路径自愈成本随库规模增长——跨进程写后
  首查曾是全量重编码（P1 已改增量对账，commit 632156f），O(N) 全库扫描
  （stats/distill）仍在。本基准给这些路径一个可重复的观测面，防止后续改动回退。
- 本机：macOS 12 x64（darwin 21.6），BGE-small-zh int8 CPU 推理（老 mac，
  编码延迟偏高一档，看趋势不看绝对值）；Python 3.12，uv wheel-only。

## 跑法

```sh
# 向量模式（需 vec extra + HF 缓存模型）
uv run --extra vec python experiments/perf-bench/bench.py --scales 100 500 1000
# 纯词面模式（未装 vec extra 自动降级，报告标注 mode）
uv run python experiments/perf-bench/bench.py --scales 100
```

数据落在系统临时目录 `compound-memory-bench/n<N>`，不动仓库与真实库；
mock 生成器可独立使用（`gen_mock.py --root <dir> --count <n>`，拒绝写真实库）。

## 场景解读

| 场景 | 测什么 | 主要成本构成 |
|---|---|---|
| seed n memories | 初始化 N 条 mock 记忆总时长（git 关闭） | 逐条同步编码 + 写文件 |
| search lexical narrow, no neighbors | 窄查询（单主题簇命中）纯检索 | 倒排查找 + 候选 parse + BM25 |
| search lexical narrow, default neighbors | 同上 + 生产默认的邻居召回 | 每 hit 一次 `find()`（rglob） |
| search lexical broad, default | 宽查询（跨簇高频词，大候选集） | 候选集线性放大以上各项 |
| search vector semantic, default | 语义改写查询（双路 RRF） | 查询编码（CPU 推理）+ KNN + 融合 |
| write + sync encode + git commit | 单条写入 | 内容编码 + 双缓存 sync + git |
| feedback, no re-encode | 使用反馈（内容 hash 不变） | 文件写 + 词法缓存全量重写 + git |
| reconcile after oob write | 带外写 1 条后的首次 search | 增量对账（scan + 只编码 diff）——P1 修复路径 |
| stats full scan | 全库统计 | rglob + 逐条 parse，O(N) |
| full rebuild-index | 双缓存全量重建 | 全量编码，O(全库)——对账的对照组 |

注意：vec 模式下所有 search 含一次查询编码（本机 ~百 ms 底噪），场景间对比
要看差值不看绝对值；纯词面模式无此底噪。semantic 场景在词面模式退化为零命中
空转（下界参考）。

## 基线（2026-10-03，vec 模式，macOS 12 x64 / BGE CPU）

| scenario | N=100 | N=500 | N=1000 |
|---|---|---|---|
| seed n memories (total) | 5.4s | 44.0s | 103.6s |
| search lexical narrow, no neighbors | 160.6ms | 322.1ms | 551.9ms |
| search lexical narrow, default neighbors | 198.2ms | 353.6ms | 587.3ms |
| search lexical broad, default | 277.4ms | 1017.8ms | 2279.9ms |
| search vector semantic, default | 142.7ms | 177.2ms | 211.4ms |
| write + sync encode + git commit | 242.7ms | 432.8ms | 291.5ms |
| feedback, no re-encode | 194.0ms | 200.7ms | 220.3ms |
| reconcile after oob write | 811.9ms | 3057.7ms | 6075.4ms |
| stats full scan (total) | 0.20s | 0.88s | 1.68s |
| full rebuild-index (total) | 6669.8ms | 19.96s | 46.39s |

### 向量召回直读 rel_path 的对照（2026-10-03，rglob 消除后）

`storage._vector_recall` 逐 hit `find()` 改为直读 knn 返回的 `rel_path` 后
（N=1000，同机同法）：

| scenario | 基线 | 直读后 |
|---|---|---|
| search lexical narrow, no neighbors | 551.9ms | 447.9ms（−19%） |
| search lexical narrow, default neighbors | 587.3ms | 477.9ms（−19%） |
| search lexical broad, default | 2279.9ms | 1772.7ms（−22%） |
| search vector semantic, default | 211.4ms | 130.9ms（−38%） |
| reconcile after oob write | 6075.4ms | 5396.2ms（−11%） |

消除的是「17 hits × rglob(N)」这一随 N 二次增长的项，N 越大收益越大；
rglob 本身比预估便宜（find 命中即返回，平均只遍历半棵目录树），候选记忆的
yaml parse 才是每次 search 固定成本的大头——这也是宽查询（大候选集）最贵的
原因。

### 词法增量对账的对照（2026-10-03，如实记录：收益有限）

词法 `Index._ensure_fresh` 的带外自愈同样改为增量对账（与向量侧同构，
对账结果与全量重建集合等价）后，reconcile 场景**没有可测改善**
（N=1000：5396ms → 5470ms，噪音内）。原因：对账消掉的只是 tokens.json
全量重写（~0.2s），路径上真正的大头是**两遍 `scan_pairs` 的 yaml parse**
（~3-4s，向量对账、词法对账各 scan 一遍）+ 全量 tokenize。词法对账仍保留：
纯词面宿主的自愈省掉全量 json 写、tokens.json 重写成本不再随 N 增长，
且两缓存机制同构。**下一个真正的杠杆**是让 scan 便宜：`parse` 的 yaml
快速路径（平面键值 frontmatter 跳过 safe_load，怪文件回退）或一次 search
内两缓存共享一遍 scan。

### parse 切 CSafeLoader 的对照（2026-10-03，全读路径受益）

micro-bench 证实 `scan_pairs` 的大头是 `yaml.safe_load`（真实 frontmatter
1.35ms/条，千条 1.35s；读文件本身仅 0.05ms/条）。换 libyaml 的
`CSafeLoader`（语义逐位一致，缺失时回退纯 Python loader）后（N=1000）：

| scenario | 上轮 | CSafeLoader |
|---|---|---|
| search lexical narrow, no neighbors | 460.2ms | 268.8ms（−42%） |
| search lexical broad, default | 1771.6ms | 1011.3ms（−43%） |
| search vector semantic, default | 123.5ms | 75.9ms（−39%） |
| reconcile after oob write | 5469.7ms | 2974.4ms（−46%） |
| stats full scan | 1.99s | 0.82s（−59%） |
| seed n memories | 94.0s | 55.1s（−41%，write 的 key 冲突检查也走 parse） |

（对照原基线，千条级累计：narrow −51%、broad −56%、semantic −64%、
reconcile −51%。）

## Windows 11 复测（2026-10-04，vec 模式，Ryzen 5 3600 / BGE CPU）

代码为当前 HEAD（已含直读 rel_path 与 CSafeLoader 两项优化，与上方 macOS
「优化后」列同代码基线）。环境：Windows 11 x64，AMD Ryzen 5 3600（6C12T）/
16GB，Python 3.12.11，BGE-small-zh int8 CPU 推理。

| scenario | N=100 | N=500 | N=1000 |
|---|---|---|---|
| seed n memories (total) | 1.3s | 11.8s | 31.2s |
| search lexical narrow, no neighbors | 29.9ms | 67.4ms | 112.2ms |
| search lexical narrow, default neighbors | 47.6ms | 81.6ms | 139.0ms |
| search lexical broad, default | 70.2ms | 216.8ms | 425.3ms |
| search vector semantic, default | 42.9ms | 45.8ms | 56.0ms |
| write + sync encode + git commit | 322.3ms | 360.1ms | 355.8ms |
| feedback, no re-encode | 286.4ms | 320.0ms | 391.8ms |
| reconcile after oob write | 182.1ms | 697.8ms | 1303.6ms |
| stats full scan (total) | 0.04s | 0.19s | 0.38s |
| full rebuild-index (total) | 793.7ms | 4014.8ms | 7725.1ms |

观察（N=1000，对照 macOS 优化后）：检索路径全面快 2–4x（narrow
268.8→112.2ms、broad 1011.3→425.3ms、reconcile 2974.4→1303.6ms、stats
0.82→0.38s）；全量编码快 ~6x（rebuild 46.4s→7.7s，对照 macOS 原始基线，
优化后未复测该项）。**write（~356ms）与 feedback（~392ms）反而高于
macOS 原始基线（291/220ms）**——Windows 的 git commit 与 sqlite fsync
开销，量级仍远低于交互阈值，观察即可。规模趋势与 macOS 一致：reconcile
随 N 线性、broad 随候选集放大、semantic 基本平坦（编码底噪主导）。

## 万条档基线（2026-10-06，vec 模式，macOS 12 x64 / BGE CPU，优化前代码）

ADR 0005 的设计规模档（先测量后优化）：下面第一表是 token stats / 共享 scan
两项优化动手**前**的基线（HEAD 292164f + bench 工具修复），第二表是优化后
同机同法对照。跑法：`--scales 10000`（数据落独立 root，seed 阶段 git 关闭）。

| scenario | N=10000 基线 |
|---|---|
| seed n memories (total) | 1673.5s |
| search lexical narrow, no neighbors | 1036.5ms |
| search lexical narrow, default neighbors | 1148.3ms |
| search lexical broad, default | 4495.7ms |
| search vector semantic, default | 102.0ms |
| write + sync encode + git commit | 576.1ms |
| feedback, no re-encode | 447.3ms |
| reconcile after oob write | 11826.6ms |
| stats full scan (total) | 2.89s |
| full rebuild-index (total) | 266.41s |

基线观察（对照 ADR 0005 三档预算）：

- **broad 4.5s 超交互预算（≤2s）**：宽查询候选 ≈ 全库（跨簇高频词命中近
  10000 条），逐候选 read+yaml parse 是成本大头——正是 token stats 的靶子。
  narrow（单簇 ≈ 500 候选）1.0s、semantic（编码底噪 + 16 条 KNN 命中）102ms
  在预算内。
- **reconcile 11.8s 超写后首查预算（≤5s）**：构成 = 词法/向量两遍 scan
  （各一遍全库 rglob+parse）+ diff 编码 + tokens.json 全量重写（15.1MB）+
  查询编码——共享 scan 消一遍。
- seed 1673.5s（~28 分钟）高于线性外推（千条 55.1s ×10 ≈ 9 分钟）：真实
  外推之外还叠加了 (a) 逐条 write 的 key 冲突扫描与 tokens.json 全量重写
  随 N 增长（每写一条重 dump 一次，末段单次 >1s），(b) 本次跑测时同机
  并行了一次全量 pytest（~3.5 分钟 CPU 竞争）。绝对值偏悲观，趋势可靠。
- 运维路径如实记录不设目标：stats 2.89s、full rebuild 266.4s（≈线性于
  千条 46.4s×10；onnx 分块编码全程未被沙箱 SIGKILL——ENCODE_CHUNK=32 的
  内存上界有效）。
- write 576ms / feedback 447ms：内含 15.1MB tokens.json 全量重写，万条档
  仍恒定在半秒级（ADR「观察」档）。

## 万条档优化对照（2026-10-06，vec 模式，macOS 12 x64，#41 token stats + 共享 scan）

同机同法对照（seed 各自独立 root）。两项手段：tokens.json v2 per-doc token
频表（候选路径免 parse 直算 BM25，emit 正文只 parse top_k）+ 词法/向量对账
共享一遍 scan。**搜索/对账行是 content_loader 修复后用同方法学在已 seed 的
同规模库上重测**（rank 曾在 emit 阶段对全部正分候选现 parse 正文，把免
parse 收益吃回大半——修复后数字才代表两项手段的真实效果；seed/write/
feedback/stats/rebuild 取自修复前代码的全量跑，这几条路径不经过 rank）。

| scenario | N=10000 基线 | N=10000 优化后 | Δ |
|---|---|---|---|
| seed n memories (total) | 1673.5s | 2745.0s | **+64%**（见下） |
| search lexical narrow, no neighbors | 1036.5ms | 113.0ms | −89% |
| search lexical narrow, default neighbors | 1148.3ms | 187.4ms | −84% |
| search lexical broad, default | 4495.7ms | **521.9ms** | −88%（≤2s 预算达标） |
| search vector semantic, default | 102.0ms | 35.3ms | −65% |
| write + sync encode + git commit | 576.1ms | 593.3ms | 持平 |
| feedback, no re-encode | 447.3ms | 521.2ms | +17% |
| reconcile after oob write | 11826.6ms | **5140.0ms** | −57%（≈5s 预算线上） |
| stats full scan (total) | 2.89s | 2.81s | 持平 |
| full rebuild-index (total) | 266.41s | 194.05s | −27%（rebuild 共享一遍 scan） |

对照观察（ADR 0005 三档预算逐条对账）：

- **交互读档达标**：broad 0.52s / narrow 0.11-0.19s / semantic 0.035s，
  全部 ≤2s。宽查询候选 ≈ 全库（~10000 条），免 parse 后剩余成本 =
  倒排查找 + per-doc 频表 lookup + BM25 + top_k 次 parse（5 次）。
  narrow default 的 187ms 里仍含每 hit 邻居 `find()` 的 rglob（ADR ①b
  明确不做的项，~1/3 成本，量级已无压力）。
- **写后首查 5.14s ≈ 5s 预算线**：构成 = 一遍 scan（全库 rglob+parse，
  ~4s，yaml parse 为主——ADR ⑤ 手写 parser 明确不做）+ diff 编码 + v2
  缓存全量重写（~22MB）+ 查询编码。本机是最慢的基准形态（macOS 12 老
  mac，BGE CPU；Windows 参考机检索路径快 2-4x），跨机看趋势：两遍 scan
  → 一遍（−57%）与机制预期一致。
- **seed +64% 是 v2 的代价，如实记录**：逐条 write 每次全量重写 tokens.json，
  v2 带 per-doc 频表后体积 15.1MB → 22MB（+45%），seed 的 O(N²) 重写项
  相应变贵；write（593ms）/feedback（521ms，+17%）同步缓涨但仍远低于
  交互阈值。灌库正门是 `batch()`（批尾一次落盘），不走这条路径；若未来
  万条级单条写变痛，候选手段是缓存分片或增量序列化，未在 #41 范围。
- 运维路径：rebuild −27%（两份缓存共享一遍 scan）；stats 不经缓存持平，
  符合「不设目标只监控」。
- 机制抽查：万条库上强制 parse 回退路径与缓存快路径的 search 输出逐位
  一致（含邻居）；v2 缓存损坏/旧版自动重建语义由测试钉住（test_index）。

## 已知观察（基线暴露，待后续处理）

- ~~向量召回的 mem 解析是 O(候选×N)~~ **已修**：直读 rel_path（见上方对照）。
  `_active_neighbors` 仍逐 hit `find()`（links 只存 id、无现成路径），5 hits
  成本约为 KNN 路的 1/3，暂留。
- **reconcile 千条级 ~3.0s**（CSafeLoader 后，原 6.1s）：剩余构成是两遍
  scan（~0.5s）+ 全量 tokenize + 向量 diff 编码 + 查询编码；再往下压需要
  两缓存共享一遍 scan（省 ~0.25s）或 token 缓存，收益已进入小头区间，
  观察即可。
- `write` 在 N=500 档高于 N=1000：git commit 时长抖动（5 样本中位数），
  非趋势，写入路径整体不随 N 显著增长。
- `feedback` ~200ms 恒定：大头是 sqlite commit fsync + 词法缓存全量重写，
  暂不随库规模显著恶化，观察即可。

