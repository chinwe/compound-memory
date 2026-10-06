"""评分：分词、词面相似度（BM25）、新近衰减、最终得分。

检索得分 = 0.70·相似度 + 0.15·置信度 + 0.10·新近度(0.5+0.5·e^(-Δt/τ)) + 0.05·类型权重

设计约束（2026-10-03 vec-spike 实测定型）：sim 是主序，先验只做 tie-break——
conf/recency/type 三槽的**有效分差跨度**必须盖不过 sim 槽的单 token 命中差，
否则高置信/新近的无关记忆会挤掉正确答案（recall-audit 失效模式②的马太效应）。
recency_score 因此带 0.5 底座（跨度 0.5），坏日期记中性值 0.5 而非 0。

rank 是排序管线的单一定义点：调用方传入原始 query 与候选记忆，
tokenize → BM25 → 归一化 → 新近 → 合分 → 排序 → 结果形状全部在实现内。
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any, NamedTuple

from .model import TYPE_SPEC, Memory

TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")

# 由 TYPE_SPEC 派生（加类型只改一张表）；.get 的兜底默认用于容错手工编辑出的未知类型
TYPE_WEIGHT = {t: s.weight for t, s in TYPE_SPEC.items()}
TAU_DAYS = {t: s.tau_days for t, s in TYPE_SPEC.items()}

W_SIM = 0.70
W_CONF = 0.15
W_RECENCY = 0.10
W_TYPE = 0.05
# 邻居召回（CONTEXT.md: 关联增值）：hit 内嵌精简邻居的形状上限——
# 数据由调用方经 neighbor_lookup 提供（store 只供活动记忆），截断/上限/去环在此单点收口
MAX_NEIGHBORS = 3
NEIGHBOR_CONTENT_CHARS = 80
# 向量路 RRF 融合（vec-spike S5 形态，2026-10-03）：两路 rank 融合为主序，
# 先验（conf/recency/type）整体压到 PRIOR_EPSILON 做 tie-break——
# RRF 相邻 rank 位差 = 1/(K+1) ≈ 0.016，ε=0.04 意味着先验最多抬 ~2 个 rank 位，
# 抬不动正确答案与高置信噪声之间的真实 rank 差（recall-audit 失效模式②的根治）。
RRF_K = 60
PRIOR_EPSILON = 0.04
# 双路先验内部的 conf/recency/type 配比（vec-spike S5 形态）：rank 打分与
# explain 分量折算共用同一组配比，勿写第二份（改先验配比只改这里）。
PRIOR_MIX_CONF = 0.5
PRIOR_MIX_RECENCY = 0.3
PRIOR_MIX_TYPE = 0.2


def _cjk_bigrams(run: list[str]) -> list[str]:
    if not run:
        return []
    if len(run) == 1:
        return list(run)
    return [run[i] + run[i + 1] for i in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """小写分词器：拉丁字母/数字整词 + CJK 相邻双字（bigram）。"""
    tokens: list[str] = []
    cjk_run: list[str] = []
    for piece in TOKEN_RE.findall(text.lower()):
        if "\u4e00" <= piece <= "\u9fff":
            cjk_run.append(piece)
        else:
            tokens.extend(_cjk_bigrams(cjk_run))
            cjk_run = []
            tokens.append(piece)
    tokens.extend(_cjk_bigrams(cjk_run))
    return tokens


def doc_text(mem: Memory) -> str:
    """记忆的可检索文本——单一定义点（content + key）。"""
    return mem.content + " " + (mem.key or "")


def age_days(date_str: str, today: dt.date) -> int | None:
    """ISO 日期字符串 → 距 today 天数；坏/缺日期返回 None（消费方决定业务动作）。"""
    try:
        return (today - dt.date.fromisoformat(date_str)).days
    except (ValueError, TypeError):
        return None


def recency_age(mem: Memory, now: dt.date) -> int | None:
    """新近基准（CONTEXT.md: recency reference）：last_used 优先，无则 created。

    返回基准距 now 的天数（负数 = 基准在未来，交由消费方定夺）；
    坏/缺日期返回 None。基准选择只在这一处，解析降级共用 age_days，
    消费方只决定 None 的业务动作（rank ⇒ 新近项记 0 分；decay ⇒ 跳过该条）。
    """
    return age_days(mem.last_used or mem.created, now)


class DocStats(NamedTuple):
    """单篇候选文档的 token 统计（#41 候选 token 源接口）。

    tf 是全量 token 频表（值 = 出现次数）、dl 是 tokenize 后的 token 总数；
    BM25 的 tf/df/dl/avgdl 全部可由它推出，与「tokenize 即时列表」数学等价。
    缓存路径（Index 的 per-doc 频表）免 parse 直接供给，词面路径由 tokenize
    现算——同一套 bm25_scores_from_stats 实现，勿再写第二份 BM25 数学。
    """

    tf: dict[str, int]
    dl: int


def doc_stats_from_tokens(tokens: list[str]) -> DocStats:
    """tokenize 列表 → DocStats（词面路径的即时统计；缓存路径直接读频表）。"""
    return DocStats(tf=dict(Counter(tokens)), dl=len(tokens))


def bm25_scores(
    query_tokens: list[str],
    docs_tokens: list[list[str]],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """各文档对 query 的 BM25 相关度（token 列表入参的兼容包装，#41 前
    的唯一形态）；数学本体在 bm25_scores_from_stats 单点。"""
    return bm25_scores_from_stats(query_tokens, [doc_stats_from_tokens(d) for d in docs_tokens], k1=k1, b=b)


def bm25_scores_from_stats(
    query_tokens: list[str],
    docs: list[DocStats],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """BM25 数学本体（单一定义点）：候选以 DocStats 频表供给。

    与 token 列表版本逐位等价——tf = 频表计数（= doc.count）、df = 含该
    token 的候选数（频表键即去重集）、dl/avgdl 同源；无匹配返回 0.0。
    """
    n_docs = len(docs)
    if n_docs == 0 or not query_tokens:
        return [0.0] * n_docs
    avgdl = sum(d.dl for d in docs) / n_docs or 1.0
    df: dict[str, int] = {}
    for doc in docs:
        for tok in doc.tf:
            df[tok] = df.get(tok, 0) + 1
    scores: list[float] = []
    for doc in docs:
        dl = doc.dl or 1
        tf = {t: c for t, c in doc.tf.items() if t in query_tokens}
        rel = 0.0
        for tok, freq in tf.items():
            idf = math.log((n_docs - df[tok] + 0.5) / (df[tok] + 0.5) + 1)
            rel += idf * freq * (k1 + 1) / (freq + k1 * (1 - b + b * dl / avgdl))
        scores.append(rel)
    return scores


def expired_by_date(valid_until: str | None, today: dt.date) -> bool:
    """valid_until 原语：字符串直判过期（#41）。

    is_expired 的 Memory 判定与词法缓存条目（无 Memory 形态，只有先验字段）
    共用这一处日期语义，勿各算各的。
    """
    if not valid_until:
        return False
    remaining = age_days(valid_until, today)
    return remaining is not None and remaining > 0


def is_expired(mem: Memory, today: dt.date) -> bool:
    """valid_until 已过 ⇒ True（valid_until 当日仍有效，次日过期）。

    坏/缺 valid_until 返回 False——坏数据不冒充过期（与 _within_days 同哲学：
    宁可少排除，不因坏日期静默吞掉一条记忆）。valid_from 不参与判定：
    检索没有 as-of 语义，未来才生效的记忆照常可召回。日期本体在
    expired_by_date（缓存条目路径复用）。
    """
    return expired_by_date(mem.valid_until, today)


def recency_score(mem: Memory, now: dt.date) -> float:
    """新近度 0.5 + 0.5·e^(-Δdays/τ)，τ 取自记忆类型；底座把槽内跨度压到 0.5（先验只做 tie-break）。

    坏/缺日期返回中性值 0.5（不奖励也不惩罚，与底座语义一致）。
    """
    tau = TAU_DAYS.get(mem.type, 90.0)
    age = recency_age(mem, now)
    if age is None:
        return 0.5
    return 0.5 + 0.5 * math.exp(-max(0, age) / tau)


def normalized_similarity(bm25: float, n_query_tokens: int) -> float:
    """BM25 除以 query token 数，截断到 [0, 1]。"""
    if n_query_tokens <= 0:
        return 0.0
    return min(1.0, bm25 / n_query_tokens)


def dup_similarity_matrix(docs: list[str]) -> list[list[float]]:
    """蒸馏疑似重复信号的相似度矩阵：每条候选文本当 query 在候选集上打分。

    matrix[i][j] = normalized_similarity(以 docs[i] 的 tokens 为 query 对 docs[j] 的 BM25)，
    与 rank 同一套分词/归一——语料语义一致，互标才可比。BM25 的 query/doc 角色不对称，
    矩阵因此非对称；对角线（自查自）恒 > 0，由消费方跳过自身。
    阈值标注（possible_dup_of）是 distill-plan 的策略，不在这里。
    """
    docs_tokens = [tokenize(d) for d in docs]
    return [
        [normalized_similarity(rel, len(qt)) for rel in bm25_scores(qt, docs_tokens)]
        for qt in docs_tokens
    ]


def final_score(sim: float, confidence: float, recency: float, mtype: str) -> float:
    return W_SIM * sim + W_CONF * confidence + W_RECENCY * recency + W_TYPE * TYPE_WEIGHT.get(mtype, 0.5)


def _ranks_by_rel(candidates: list[Memory], rels: list[float]) -> dict[str, int]:
    """rel 降序的 1-based rank 表：RRF 词面路与 explain 分量共用同一构造（单一拷贝）。"""
    ordered = sorted(((m.id, rel) for m, rel in zip(candidates, rels) if rel > 0), key=lambda t: -t[1])
    return {mid: r for r, (mid, _) in enumerate(ordered, 1)}


def rank(
    query: str,
    candidates: list[Memory],
    now: dt.date,
    top_k: int = 5,
    neighbor_lookup: Callable[[str], list[Memory]] | None = None,
    vec_sims: dict[str, float] | None = None,
    doc_stats: Sequence[DocStats | None] | None = None,
    content_loader: Callable[[str], str] | None = None,
    explain: bool = False,
) -> list[dict[str, Any]]:
    """排序管线：query 与候选记忆进，最终搜索结果出。

    结果 dict 的形状在这里一处定义（id / score / similarity / confidence /
    uses / type / ns / source / content；提供 neighbor_lookup 时每 hit 内嵌
    neighbors）。邻居只"带出"不"提分"——公式与排序不受影响（#7）。

    vec_sims（mem_id → 余弦相似度）为 None 时走纯词面单路：rel≤0 的候选缺席、
    BM25 归一分进 0.70 槽——与历史行为逐位一致。提供时走双路 RRF 融合：
    词面路（rel>0 才参与）与向量路各出一列 rank，RRF norm 作主序、先验压到
    PRIOR_EPSILON 做 tie-break；词面零命中但向量召回的候选由此进入结果。
    similarity 字段语义随路径标注在 explain.path：单路 = BM25 归一分，
    双路 = 归一化 RRF 融合分（fused/rrf_max）——字段名不动（默认 hit 形状
    红线），语义歧义由 opt-in 分量消解（#44）。

    doc_stats（#41 候选 token 源）：与 candidates 对齐的 per-doc 频表；提供位
    免 tokenize（缓存路径），None 位与不传整体都回退 tokenize(doc_text)。
    content_loader（mem_id → 正文）：提供时命中的 content 经它现取（缓存候选
    的正文延迟 parse），缺省取 mem.content——两条路径输出逐位一致。

    explain 是 opt-in 排障面（#44，ADR-0008 展示边界）：True 时每个 hit 附加
    `explain` 分量对象（检索通道 lexical/vector/both、路径 linear/rrf、词面/
    向量 rank、RRF 原始分、先验原始输入与按权重折算的 terms）；terms 之和与
    score 在 epsilon 内对账，两条路径同构。缺省 False 输出形状逐位不变。
    """
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    stats: list[DocStats] = []
    for i, mem in enumerate(candidates):
        cached = doc_stats[i] if doc_stats is not None else None
        stats.append(cached if cached is not None else doc_stats_from_tokens(tokenize(doc_text(mem))))
    rels = bm25_scores_from_stats(q_tokens, stats)
    by_id = {m.id: (m, rel) for m, rel in zip(candidates, rels)}
    hits: list[dict[str, Any]] = []

    def emit(mem: Memory, sim: float, score: float, expl: dict[str, Any] | None = None) -> None:
        # content 先占位：emit 会发生在每个正分候选上（不止 top_k），正文
        # 现取（parse）必须推迟到切片后——否则宽查询把省下的候选 parse 又
        # 在 emit 里全数吃回（perf-bench #41 实测教训）
        hit: dict[str, Any] = {
            "id": mem.id,
            "score": round(score, 4),
            "similarity": round(sim, 4),
            "confidence": mem.confidence,
            "uses": mem.uses,
            "type": mem.type,
            "ns": mem.ns,
            "source": mem.source,
            "content": "" if content_loader is not None else mem.content,
        }
        if expl is not None:
            hit["explain"] = expl
        hits.append(hit)

    if vec_sims is None:
        lex_ranks = _ranks_by_rel(candidates, rels) if explain else None
        for mem, rel in zip(candidates, rels):
            if rel <= 0:
                continue
            sim = normalized_similarity(rel, len(q_tokens))
            rec = recency_score(mem, now)
            tw = TYPE_WEIGHT.get(mem.type, 0.5)
            score = final_score(sim, mem.confidence, rec, mem.type)
            expl = None
            if explain and lex_ranks is not None:
                # 单路：final_score 四分量各按权重折算，terms 之和即 score
                expl = {
                    "channel": "lexical",
                    "path": "linear",
                    "lexical_rank": lex_ranks[mem.id],
                    "vector_rank": None,
                    "rrf_score": None,
                    "similarity": round(sim, 4),
                    "recency_score": round(rec, 4),
                    "type_weight": tw,
                    "terms": {
                        "similarity": round(W_SIM * sim, 4),
                        "confidence": round(W_CONF * mem.confidence, 4),
                        "recency": round(W_RECENCY * rec, 4),
                        "type": round(W_TYPE * tw, 4),
                    },
                }
            emit(mem, sim, score, expl)
    else:
        lexical_rank = _ranks_by_rel(candidates, rels)
        vec_rank = {
            mid: r
            for r, (mid, _) in enumerate(
                sorted(((mid, s) for mid, s in vec_sims.items() if mid in by_id), key=lambda t: -t[1]), 1
            )
        }
        rrf_max = 2.0 / (RRF_K + 1)  # 双路都拿 rank1 的理论上限；满命中归一到 1.0
        for mem, _ in by_id.values():
            fused = 0.0
            if mem.id in lexical_rank:
                fused += 1.0 / (RRF_K + lexical_rank[mem.id])
            if mem.id in vec_rank:
                fused += 1.0 / (RRF_K + vec_rank[mem.id])
            if fused <= 0:
                continue  # 两路都不在场：不该出现在结果里（调用方候选并集含兜底）
            rec = recency_score(mem, now)
            rec_n = (rec - 0.5) / 0.5  # 底座归一回 [0,1]
            tw = TYPE_WEIGHT.get(mem.type, 0.5)
            prior = PRIOR_MIX_CONF * mem.confidence + PRIOR_MIX_RECENCY * rec_n + PRIOR_MIX_TYPE * tw
            sim = fused / rrf_max
            expl = None
            if explain:
                # 双路：similarity 槽即归一化融合分，先验三分量按 ε×配比折算，
                # terms 之和 = sim + ε·prior = score
                if mem.id in lexical_rank and mem.id in vec_rank:
                    channel = "both"
                elif mem.id in lexical_rank:
                    channel = "lexical"
                else:
                    channel = "vector"
                expl = {
                    "channel": channel,
                    "path": "rrf",
                    "lexical_rank": lexical_rank.get(mem.id),
                    "vector_rank": vec_rank.get(mem.id),
                    "rrf_score": round(fused, 4),
                    "similarity": round(sim, 4),
                    "recency_score": round(rec, 4),
                    "type_weight": tw,
                    "terms": {
                        "similarity": round(sim, 4),
                        "confidence": round(PRIOR_EPSILON * PRIOR_MIX_CONF * mem.confidence, 4),
                        "recency": round(PRIOR_EPSILON * PRIOR_MIX_RECENCY * rec_n, 4),
                        "type": round(PRIOR_EPSILON * PRIOR_MIX_TYPE * tw, 4),
                    },
                }
            emit(mem, sim, sim + PRIOR_EPSILON * prior, expl)
    hits.sort(key=lambda h: -h["score"])
    top = hits[:top_k]
    if content_loader is not None:
        # 只对返回的 top_k 现取正文（top_k 次 parse，与候选集大小无关）
        for hit in top:
            hit["content"] = content_loader(hit["id"])
    if neighbor_lookup is not None:
        for hit in top:
            hit["neighbors"] = [
                {
                    "id": n.id,
                    "content": n.content[:NEIGHBOR_CONTENT_CHARS] + ("…" if len(n.content) > NEIGHBOR_CONTENT_CHARS else ""),
                    "type": n.type,
                    "ns": n.ns,
                }
                for n in neighbor_lookup(hit["id"])
                if n.id != hit["id"]  # 去环：双向 link 不把 hit 自己带回来
            ][:MAX_NEIGHBORS]
    return top
