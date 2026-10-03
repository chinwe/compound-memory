# Spike：zvec vs sqlite-vec 作为 scoring.rank sim 通道的对比

- 日期: 2026-10-03
- 背景: spec 0001 将向量索引列为唯一 deferred 项（触发条件：活动记忆 ≥500 条或实际报告召回缺口）；技术路线已验证为 sqlite-vec + BGE-small-zh int8 + RRF（[vector-search-ground](../../docs/wayfinder/research/vector-search-ground.md)，2026-10-01）。阿里 2026-09 开源 zvec/zvec-grep 后需重开引擎对比。
- 本机: macOS 12 x64（darwin 21.6），Python 3.12.13，uv wheel-only（`UV_ONLY_BINARY=':all:'`）
- 数据: 真实记忆库活动区 32 条（read-only，`MemoryStore.parse` 直读）；10 条任务式查询，人工标注 expected（spike 级标注，结果为方向性证据而非基准）

## 结论速览

| 问题 | 结论 |
|---|---|
| zvec Python SDK 本机可用？ | **否——事实性排除**。PyPI 全部版本无 macOS x64 wheel、无 sdist（uv 安装实锤失败），源码构建违背 wheel-only 约束。与 torch/llama-cpp-python 同判 |
| sqlite-vec 本机可用？ | 是。0.1.9 wheel 128KB，加载/KNN/与 numpy 全量余弦 top-5 逐查询一致，直接可用 |
| 向量路修复召回缺口？ | **是**。RRF 融合 MRR 0.79→0.95，hit@3 0.80→1.00；2026-10-02 审计的词面失效案例（姓名/城市查询）由 rank-4 修复到 rank-1 |
| 把向量/RRF 分塞进现 sim 槽？ | **全部为负收益**（S1 MRR 0.28 / S3 0.50 / S4 两阶段 0.52，均低于 S0 现状 0.78）。瓶颈不在 sim 通道，在 final_score 的 conf/recency 绝对分差压过 sim 槽区分度 |
| 建议 | 不做"替换 sim 通道"；值得做的是「合分公式校准（审计②遗留）+ RRF 召回扩候选」的组合演进，见下文设计建议 |

## 1. 引擎可得性核验（本机事实）

| 维度 | sqlite-vec 0.1.9 | zvec 0.7.0（Python） | zvec 0.7.1（Node，zg 底座） |
|---|---|---|---|
| 本机安装 | ✅ 128KB wheel（`macosx_10_6_x86_64`） | ❌ 无 macOS x64 wheel、无 sdist | ✅ optionalDeps 含 `@zvec/bindings-darwin-x64` |
| 形态 | SQLite loadable 扩展 + vec0 虚拟表 | 嵌入式库，自有目录格式 + WAL | npm 包（zg CLI + MCP） |
| 检索能力 | 纯向量暴力扫描（千条级毫秒，ANN 在 roadmap） | dense+sparse+FTS 混合、HNSW/DiskANN | 同左 |
| 许可 | MIT / Apache-2.0 | Apache-2.0 | Apache-2.0 |

zvec 安装失败的 uv 报错要点（实锤）：

```
hint: Wheels are available for `zvec` (v0.7.0) on the following platforms:
`manylinux_2_28_x86_64`, ..., `macosx_11_0_arm64`, `win_amd64`
```

（0.6.0 wheel 清单同构；PyPI JSON API 核验无 `.tar.gz`。）

**定位结论**：zvec 家族对本仓（Python）当前只有"外部工具"价值——zg CLI 本机可跑，可给任意工作区（含记忆库）提供语义检索旁路，但不可能嵌入 `scoring.rank` 管线。嵌入路线唯一可行引擎是 sqlite-vec，与 spec 既定路线一致。zvec 若未来发布 macOS x64 wheel 可重开评估。

## 2. sqlite-vec 接入验证

- 冒烟：`vec_version()`=v0.1.9；vec0 表 + KNN 正确；与 numpy 全量余弦 **top-5 逐查询一致**（10/10 断言通过）——引擎替换不改变排序语义。
- vec0 `MATCH` 返回归一化向量上的 L2 距离，余弦需按 `1 − d²/2` 换算（接入时封装在向量通道内部）。
- 延迟（n=32）：KNN 平均 2.4–3.6ms；BM25 现状路径 8.8–12.7ms——向量路不劣于现状。

### embedding 延迟（重要修正）

BGE-small-zh-v1.5 ONNX int8（24MB，HF 本地缓存已存在，零下载）+ onnxruntime 1.19.2 + tokenizers：

| 场景 | 实测 | 调研笔记旧估 | 差异原因 |
|---|---|---|---|
| 查询编码（短文本） | 13–19ms | — | — |
| **单条记忆编码**（850 字符） | **374ms** | 8.3ms（30 字短文本） | 真实记忆条目长度 ≈28× 测试文本 |
| 全库重建（32 条 batch） | 11.3s | 8.3s/千条 | 同上 + batch padding 到最长 |

**写入路径影响**：单条写入同步编码 ≈400ms 级——serverless 10s 窗口内安全，但 MCP `memory_write` 响应延迟能感知。全量重建千条级为分钟量级（可接受，索引即缓存）。

## 3. 召回评测（10 查询 × 32 条真实记忆）

三路纯召回（不含 conf/recency/type 合分）：

| 路 | hit@1 | hit@3 | MRR |
|---|---|---|---|
| BM25（现状 sim 通道） | 0.70 | 0.80 | 0.79 |
| 向量（sqlite-vec + BGE） | 0.80 | **1.00** | 0.90 |
| RRF 融合（k=60） | **0.90** | **1.00** | **0.95** |

要点：
- 向量路把全部 expected 拉进 top-3，修复审计③的词面盲区（「用户叫什么名字/所在城市」vs「用户姓名…位于杭州」：BM25 rank-4 → 向量 rank-1）。
- RRF 融合对两路互补最敏感（论文结论 "RRF almost invariably improved on the best of the combined results" 在本仓数据上复现）。
- 词面对照组（邮件发件人、MCP 破坏性变更）两路都 rank-1——向量路没有伤害词面强命中。

### 合分形态（都过 `final_score`，其余权重不动）

| 形态 | hit@1 | hit@3 | MRR |
|---|---|---|---|
| S0 现状（BM25 sim） | 0.70 | 0.80 | 0.78 |
| S1 替换为向量 sim | 0.10 | 0.30 | 0.28 |
| S2 槽内 max(BM25, 向量) | 0.50 | 0.60 | 0.60 |
| S3 RRF 分缩放进 sim 槽 | 0.30 | 0.50 | 0.50 |
| S4 两阶段：RRF 选 top-8 池，池内合分 | 0.30 | 0.60 | 0.52 |

**为什么全部失败**：BM25 的 `normalized_similarity` 天然稀疏（绝大多数候选 ≈0），与线性加权合分兼容；向量分数密集（BGE 分布集中），填满 sim 槽后 0.45 槽内区分度消失，而 conf 槽跨度最高 0.125、recency 槽 0.20、type 槽 0.02——合分被马太效应主导（审计②）。S4 的候选池限制也救不回来：池内条目 RRF 分彼此接近，次级排序仍由 conf/recency 决定。

### 与两次审计的互证

- 审计③（词面无同义匹配）：向量路定量证实可修复 ✅
- 审计②（conf 马太效应）：S1–S4 全面劣化定量证实了它是**合分公式层面的瓶颈**，换 sim 引擎/融合方式都绕不开 ❌→ 必须先修

## 4. 接入设计建议（若推进）

1. **顺序**：先做合分公式校准（压缩 conf/recency 的绝对分差跨度，或改为资格线 + 池内 tie-break），再接向量召回；单独替换 sim 通道在任何形态下都是负收益，不做。
2. **推荐形态**：candidates 扩为「词面候选 ∪ 向量 top-k」→ RRF 融合定资格与主序 → 合分公式（校准后）做池内次级排序。RRF 与向量路都在 `rank`（单一定义点）内实现。
3. **向量索引作为 Index 的姊妹缓存**：遵守同一不变量（活动必在、归档必不在、损坏全量重建）；vec0 表 + 向量落 sqlite 文件（如 `index/vectors.db`）；手编内容重建走显式 `rebuild-index`（与现有行为一致）。
4. **写入编码成本**（≈400ms/条）需要独立决策：同步编码（简单、写延迟可感）vs 读路径惰性补编码（写快、首查慢）vs 后台批（复杂）。MCP 5 tool 边界不受影响。
5. **触发条件现状**：32 条 << 500 条；召回缺口已被审计③ + 本 spike 定性定量证实。是否现在投入，是取舍票——本 spike 的证据支持"做组合演进"而非"做引擎替换"。

## 复现

```sh
# 依赖不进 pyproject，--with 注入临时环境；记忆库只读
UV_ONLY_BINARY=':all:' uv run --python 3.12 \
  --with "onnxruntime==1.19.2" --with tokenizers --with numpy --with sqlite-vec \
  python experiments/vec-spike/spike_eval.py
```

## 来源

- [vector-search-ground](../../docs/wayfinder/research/vector-search-ground.md)（sqlite-vec/BGE/onnxruntime 平台事实与旧延迟基线）
- [alibaba/zvec](https://github.com/alibaba/zvec) · [zvec-ai/zvec-grep](https://github.com/zvec-ai/zvec-grep) · PyPI/npm JSON API wheel 清单核验（2026-10-03）
- 记忆：`proj-compound-memory-recall-audit`（2026-10-02 召回审计）、`proj-zvec-zg`（zg 调研）
