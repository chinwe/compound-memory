"""Vec-spike: zvec vs sqlite-vec as the sim channel for scoring.rank.

Runs on real memory-store data (read-only) and answers:
  1. Which engines are actually installable on this machine (macOS 12 x64)?
  2. Does a vector channel fix the lexical-BM25 recall gaps found in the
     2026-10-02 recall audit (insight 20261002_9ea00a)?
  3. How should the vector channel enter final_score: replace BM25 (S1),
     slot-max fuse (S2), or RRF fuse (S3)?
  4. Latency of each step at real store scale.

Run (from repo root, wheel-only, no pyproject changes):
  UV_ONLY_BINARY=':all:' uv run --python 3.12 \
    --with "onnxruntime==1.19.2" --with tokenizers --with numpy --with sqlite-vec \
    python experiments/vec-spike/spike_eval.py

Eval queries are annotated by hand (spike-grade labels, one or two expected
memory ids per query); results are directional evidence, not a benchmark.
"""

from __future__ import annotations

import glob
import sqlite3
import time
from datetime import date
from pathlib import Path

import numpy as np

from compound_memory.model import Memory
from compound_memory.scoring import (
    TYPE_WEIGHT as TYPE_W,
    bm25_scores,
    doc_text,
    final_score,
    normalized_similarity,
    recency_score,
    tokenize,
)
from compound_memory.storage import MemoryStore

MEM_ROOT = Path.home() / ".agents/memory"
TODAY = date(2026, 10, 3)  # 固定评测日，与 conftest CLOCK_DATE 同思路，保证可复现
KNN_TABLE = "mem_vec"
RRF_K = 60

# 评测查询集：真实任务式提问 + 同义改写；expected 是人工标注的相关记忆 id
EVAL_QUERIES: list[dict[str, object]] = [
    {"q": "用户叫什么名字，在哪个城市", "expected": ["20261001_75e73d"]},          # 审计③原案例：姓名/城市 vs 位于某市
    {"q": "Python 项目里安装依赖包很慢或者装不上怎么办", "expected": ["20261001_b6c5d2"]},  # 审计 type 权重案例
    {"q": "生成一段中文配音，用哪个声音", "expected": ["20261001_54d5a8"]},          # 同义：配音/声音 vs TTS/语音
    {"q": "这个系统部署在哪个平台，接口超时了怎么处理", "expected": ["20261001_be3660"]},
    {"q": "发通知邮件的发件人地址是什么", "expected": ["20261001_c3f431"]},          # 词面对照组：BM25 应命中
    {"q": "小孩的学校相关文件放在哪个网盘", "expected": ["20261001_d41da0"]},        # 同义：网盘 vs 腾讯文档/飞书
    {"q": "和这个用户协作时输出格式上要注意什么", "expected": ["20261001_e015cf"]},
    {"q": "升级 MCP 库的时候有哪些破坏性变更要注意", "expected": ["20261001_8a0c23"]},  # 词面对照组
    {"q": "想让搜索结果顺便带出相关联的其他记忆", "expected": ["20261001_f0ff4c", "20261001_485242"]},
    {"q": "测试里异步 client 的 fixture 为什么会崩", "expected": ["20261001_a7d9c7"]},
]


def load_active_memories() -> list[Memory]:
    """只读解析活动区记忆文件，不经过 store 写路径（不触发 git/索引）。"""
    files = sorted((MEM_ROOT / "namespaces").rglob("*.md"))
    return [MemoryStore.parse(p) for p in files]


# ---------- embedding（BGE-small-zh int8，HF 本地缓存，零网络） ----------

def load_encoder():
    import onnxruntime as ort
    from tokenizers import Tokenizer

    snaps = sorted(glob.glob(str(Path.home() / ".cache/huggingface/hub/models--Xenova--bge-small-zh-v1.5/snapshots/*/")))
    assert snaps, "BGE-small-zh ONNX snapshot not found in HF cache"
    snap = Path(snaps[-1])
    tok = Tokenizer.from_file(str(snap / "tokenizer.json"))
    tok.enable_truncation(max_length=512)
    tok.enable_padding()
    sess = ort.InferenceSession(str(snap / "onnx/model_quantized.onnx"), providers=["CPUExecutionProvider"])
    names = {i.name for i in sess.get_inputs()}

    def encode(texts: list[str]) -> np.ndarray:
        encs = tok.encode_batch(texts)
        feed = {
            "input_ids": np.array([e.ids for e in encs], dtype=np.int64),
            "attention_mask": np.array([e.attention_mask for e in encs], dtype=np.int64),
            "token_type_ids": np.array([e.type_ids for e in encs], dtype=np.int64),
        }
        out = sess.run(None, {k: v for k, v in feed.items() if k in names})[0]
        cls = out[:, 0, :]  # BGE 约定：[CLS] 表示 + L2 归一化
        return cls / np.linalg.norm(cls, axis=1, keepdims=True)

    return encode


# ---------- 向量路：sqlite-vec（真实接入路径）+ numpy 交叉验证 ----------

class SqliteVecChannel:
    """sqlite-vec vec0 表承载全部活动记忆向量；查询走 SQL KNN。"""

    def __init__(self, dim: int) -> None:
        import sqlite_vec

        self.db = sqlite3.connect(":memory:")
        self.db.enable_load_extension(True)
        sqlite_vec.load(self.db)
        self.db.enable_load_extension(False)
        self.db.execute(f"CREATE VIRTUAL TABLE {KNN_TABLE} USING vec0(embedding float[{dim}])")
        self._counter = 0

    def bulk_insert(self, vecs: np.ndarray) -> float:
        import sqlite_vec

        rows = [(i, sqlite_vec.serialize_float32(v.tolist())) for i, v in enumerate(vecs)]
        t0 = time.perf_counter()
        self.db.executemany(f"INSERT INTO {KNN_TABLE}(rowid, embedding) VALUES (?, ?)", rows)
        return (time.perf_counter() - t0) * 1000

    def knn(self, qvec: np.ndarray, k: int) -> tuple[list[tuple[int, float]], float]:
        """返回 [(rowid, cosine)]（cosine = 1 - d²/2，d 为归一化向量 L2 距离）与耗时 ms。"""
        import sqlite_vec

        t0 = time.perf_counter()
        rows = self.db.execute(
            f"SELECT rowid, distance FROM {KNN_TABLE} WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (sqlite_vec.serialize_float32(qvec.tolist()), k),
        ).fetchall()
        dt = (time.perf_counter() - t0) * 1000
        return [(int(r[0]), 1.0 - (r[1] ** 2) / 2.0) for r in rows], dt


# ---------- 评测三路召回与四种合分形态 ----------

def rrf_fuse(rankings: list[dict[str, int]], n: int) -> dict[str, float]:
    """标准 RRF：score = Σ 1/(k + rank)，rank 从 1 起；某路缺席的条目不计该项。"""
    out: dict[str, float] = {m.id: 0.0 for m in ALL}
    for ranking in rankings:
        for mem_id, rank in ranking.items():
            out[mem_id] += 1.0 / (RRF_K + rank)
    return out


def ranks_of(hits: list[tuple[str, float]]) -> dict[str, int]:
    return {mid: i + 1 for i, (mid, _) in enumerate(hits)}


def expected_rank(rank_map: dict[str, int], expected: list[str]) -> int | None:
    rs = [rank_map.get(e) for e in expected if rank_map.get(e)]
    return min(rs) if rs else None


def metrics(rows: list[dict[str, object]], col: str) -> tuple[float, float, float]:
    """hit@1 / hit@3 / MRR（按 col 列记录的 expected 最佳排名）。"""
    hits1 = hits3 = 0
    rr_sum = 0.0
    for row in rows:
        r = row[col]  # type: ignore[index]
        if r == 1:
            hits1 += 1
        if r is not None and r <= 3:
            hits3 += 1
        if r is not None:
            rr_sum += 1.0 / r
    n = len(rows)
    return hits1 / n, hits3 / n, rr_sum / n


ALL: list[Memory] = []


def main() -> None:
    global ALL
    mems = load_active_memories()
    ALL = mems
    print(f"store: {len(mems)} active memories at {MEM_ROOT}")

    encode = load_encoder()

    # ---- 索引侧：全库编码 + 双路引擎装载 ----
    docs = [doc_text(m) for m in mems]
    t0 = time.perf_counter()
    doc_vecs = encode(docs)
    enc_full_ms = (time.perf_counter() - t0) * 1000

    ch = SqliteVecChannel(doc_vecs.shape[1])
    ins_ms = ch.bulk_insert(doc_vecs)
    print(f"index: encode {len(mems)} docs = {enc_full_ms:.0f} ms | sqlite-vec bulk insert = {ins_ms:.1f} ms")

    # 单条长文档编码延迟：回答"写入路径同步编码是否无压力"（全量 batch 会 pad 到最长，不代表单条成本）
    longest = max(docs, key=len)
    single_ms = []
    for _ in range(3):
        t0 = time.perf_counter()
        encode([longest])
        single_ms.append((time.perf_counter() - t0) * 1000)
    n_tokens = len(tokenize(longest))
    print(f"single-doc encode: {len(longest)} chars / ~{n_tokens}+ tokens -> {min(single_ms):.0f} ms (best of 3)")

    # ---- 逐查询评测 ----
    rows: list[dict[str, object]] = []
    q_enc_ms: list[float] = []
    knn_ms: list[float] = []
    bm25_ms: list[float] = []

    for item in EVAL_QUERIES:
        q, expected = str(item["q"]), list(item["expected"])  # type: ignore[arg-type]

        t0 = time.perf_counter()
        qvec = encode([q])[0]
        q_enc_ms.append((time.perf_counter() - t0) * 1000)

        knn, dt = ch.knn(qvec, k=len(mems))
        knn_ms.append(dt)
        vec_hits = [(mems[i].id, cos) for i, cos in knn]

        # 交叉验证：sqlite-vec 结果必须与 numpy 全量余弦一致（引擎替换不改变排序）
        np_sims = doc_vecs @ qvec
        np_order = [mems[i].id for i in np.argsort(-np_sims)[:5]]
        sv_order = [m for m, _ in vec_hits[:5]]
        assert np_order == sv_order, f"engine mismatch on: {q}\n{np_order}\n{sv_order}"

        t0 = time.perf_counter()
        q_tokens = tokenize(q)
        doc_tokens = [tokenize(d) for d in docs]
        rels = bm25_scores(q_tokens, doc_tokens)
        bm25_ms.append((time.perf_counter() - t0) * 1000)
        bm25_hits = [
            (m.id, normalized_similarity(rel, len(q_tokens)))
            for m, rel in zip(mems, rels)
            if rel > 0  # 现状 rank 行为：零相关直接缺席
        ]
        bm25_hits.sort(key=lambda h: -h[1])
        vec_hits_sorted = sorted(vec_hits, key=lambda h: -h[1])

        bm25_rank = ranks_of(bm25_hits)
        vec_rank = ranks_of(vec_hits_sorted)
        rrf = rrf_fuse([bm25_rank, vec_rank], len(mems))
        rrf_hits = sorted(rrf.items(), key=lambda kv: -kv[1])

        def fused_final(sim_of: dict[str, float]) -> dict[str, int]:
            # 合分形态只改 sim 通道的来源；recency/conf/type 与现状完全一致（复用 scoring 单一定义点）
            scored = [
                (m.id, final_score(sim_of.get(m.id, 0.0), m.confidence, recency_score(m, TODAY), m.type))
                for m in mems
            ]
            scored.sort(key=lambda t: -t[1])
            return ranks_of(scored)

        # 合分形态：S0 现状 BM25 | S1 替换为向量 | S2 槽内 max 融合 | S3 RRF 缩放进 sim 槽
        # S4 两阶段：RRF 先选 top-M 候选池，池内再按 final_score 合分（对应 store 的 candidates→rank 真实接入形态）
        vec_sim = {mid: max(0.0, cos) for mid, cos in vec_hits}
        bm25_sim = {mid: s for mid, s in bm25_hits}
        rrf_max = 2.0 / (RRF_K + 1)
        s0 = fused_final(bm25_sim)
        s1 = fused_final(vec_sim)
        s2 = fused_final({m.id: max(bm25_sim.get(m.id, 0.0), vec_sim.get(m.id, 0.0)) for m in mems})
        s3 = fused_final({mid: v / rrf_max for mid, v in rrf.items()})
        pool = {mid for mid, _ in rrf_hits[:8]}
        s4 = {
            mid: rank
            for mid, rank in fused_final({mid: rrf[mid] / rrf_max for mid in pool}).items()
            if mid in pool
        }

        # S5: RRF norm 为主序，先验整体压到 ε 量级做 tie-break（ε=0.04 ≈ 2 个 RRF rank 位）
        # prior_norm = 0.5·conf + 0.3·recency(底座归一) + 0.2·type_weight，各因子 [0,1]
        eps = 0.04
        s5_scored = []
        for m in mems:
            rec = recency_score(m, TODAY)
            rec_n = (rec - 0.5) / 0.5  # 底座归一回 [0,1]
            prior = 0.5 * m.confidence + 0.3 * rec_n + 0.2 * TYPE_W[m.type]
            s5_scored.append((m.id, rrf[m.id] / rrf_max + eps * prior))
        s5_scored.sort(key=lambda t: -t[1])
        s5 = ranks_of(s5_scored)

        rows.append({
            "q": q,
            "expected": ",".join(expected),
            "bm25": expected_rank(bm25_rank, expected),
            "vec": expected_rank(vec_rank, expected),
            "rrf": expected_rank(ranks_of(rrf_hits), expected),
            "S0": expected_rank(s0, expected),
            "S1": expected_rank(s1, expected),
            "S2": expected_rank(s2, expected),
            "S3": expected_rank(s3, expected),
            "S4": expected_rank(s4, expected),
            "S5": expected_rank(s5, expected),
        })

    # ---- 汇总输出 ----
    print("\nper-query best rank of expected memory (— = missed entirely):")
    print("| query | expected | bm25 | vec | rrf | S0 raw | S1 replace | S2 slotmax | S3 rrf | S4 2-stage | S5 rrf+tiebreak |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        cells = " | ".join("—" if r[c] is None else str(r[c]) for c in ["bm25", "vec", "rrf", "S0", "S1", "S2", "S3", "S4", "S5"])  # type: ignore[index]
        print(f"| {r['q']} | {r['expected']} | {cells} |")

    print("\nsummary (recall channels and fusion forms):")
    print(f"{'':12} {'hit@1':>6} {'hit@3':>6} {'MRR':>6}")
    for col in ["bm25", "vec", "rrf", "S0", "S1", "S2", "S3", "S4", "S5"]:
        h1, h3, mrr = metrics(rows, col)  # type: ignore[arg-type]
        print(f"{col:12} {h1:6.2f} {h3:6.2f} {mrr:6.2f}")

    print("\nlatency:")
    print(f"  encode query (single)  avg {sum(q_enc_ms)/len(q_enc_ms):.1f} ms")
    print(f"  sqlite-vec KNN n={len(mems)}    avg {sum(knn_ms)/len(knn_ms):.2f} ms")
    print(f"  BM25 tokenize+score    avg {sum(bm25_ms)/len(bm25_ms):.2f} ms")
    print(f"  full-reindex encode    {enc_full_ms:.0f} ms ({len(mems)} docs)")
    print("\nconsistency: sqlite-vec vs numpy cosine — asserted identical top-5 for all queries")
    print("SPIKE EVAL OK")


if __name__ == "__main__":
    main()
