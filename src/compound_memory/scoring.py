"""评分：分词、词面相似度（BM25）、新近衰减、最终得分。

检索得分 = 0.45·相似度 + 0.25·置信度 + 0.20·新近度(e^(-Δt/τ)) + 0.10·类型权重

rank 是排序管线的单一定义点：调用方传入原始 query 与候选记忆，
tokenize → BM25 → 归一化 → 新近 → 合分 → 排序 → 结果形状全部在实现内。
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections.abc import Callable
from typing import Any

from .model import TYPE_SPEC, Memory

TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")

# 由 TYPE_SPEC 派生（加类型只改一张表）；.get 的兜底默认用于容错手工编辑出的未知类型
TYPE_WEIGHT = {t: s.weight for t, s in TYPE_SPEC.items()}
TAU_DAYS = {t: s.tau_days for t, s in TYPE_SPEC.items()}

W_SIM = 0.45
W_CONF = 0.25
W_RECENCY = 0.20
W_TYPE = 0.10
# 邻居召回（CONTEXT.md: 关联增值）：hit 内嵌精简邻居的形状上限——
# 数据由调用方经 neighbor_lookup 提供（store 只供活动记忆），截断/上限/去环在此单点收口
MAX_NEIGHBORS = 3
NEIGHBOR_CONTENT_CHARS = 80


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


def recency_age(mem: Memory, now: dt.date) -> int | None:
    """新近基准（CONTEXT.md: recency reference）：last_used 优先，无则 created。

    返回基准距 now 的天数（负数 = 基准在未来，交由消费方定夺）；
    坏/缺日期返回 None。选基准与解析只在这一处，消费方只决定 None 的业务动作
    （rank ⇒ 新近项记 0 分；decay ⇒ 跳过该条）。
    """
    ref = mem.last_used or mem.created
    try:
        return (now - dt.date.fromisoformat(ref)).days
    except (ValueError, TypeError):
        return None


def bm25_scores(
    query_tokens: list[str],
    docs_tokens: list[list[str]],
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """各文档对 query 的 BM25 相关度；无匹配时返回 0.0。"""
    n_docs = len(docs_tokens)
    if n_docs == 0 or not query_tokens:
        return [0.0] * n_docs
    avgdl = sum(len(d) for d in docs_tokens) / n_docs or 1.0
    df: dict[str, int] = {}
    for doc in docs_tokens:
        for tok in set(doc):
            df[tok] = df.get(tok, 0) + 1
    scores: list[float] = []
    for doc in docs_tokens:
        dl = len(doc) or 1
        tf = {t: doc.count(t) for t in set(doc) if t in query_tokens}
        rel = 0.0
        for tok, freq in tf.items():
            idf = math.log((n_docs - df[tok] + 0.5) / (df[tok] + 0.5) + 1)
            rel += idf * freq * (k1 + 1) / (freq + k1 * (1 - b + b * dl / avgdl))
        scores.append(rel)
    return scores


def recency_score(mem: Memory, now: dt.date) -> float:
    """指数新近度 e^(-Δdays/τ)，τ 取自记忆类型；坏日期 ⇒ 0.0。"""
    tau = TAU_DAYS.get(mem.type, 90.0)
    age = recency_age(mem, now)
    if age is None:
        return 0.0
    return math.exp(-max(0, age) / tau)


def normalized_similarity(bm25: float, n_query_tokens: int) -> float:
    """BM25 除以 query token 数，截断到 [0, 1]。"""
    if n_query_tokens <= 0:
        return 0.0
    return min(1.0, bm25 / n_query_tokens)


def final_score(sim: float, confidence: float, recency: float, mtype: str) -> float:
    return W_SIM * sim + W_CONF * confidence + W_RECENCY * recency + W_TYPE * TYPE_WEIGHT.get(mtype, 0.5)


def rank(
    query: str,
    candidates: list[Memory],
    now: dt.date,
    top_k: int = 5,
    neighbor_lookup: Callable[[str], list[Memory]] | None = None,
) -> list[dict[str, Any]]:
    """排序管线：query 与候选记忆进，最终搜索结果出。

    结果 dict 的形状在这里一处定义（id / score / similarity / confidence /
    uses / type / ns / source / content；提供 neighbor_lookup 时每 hit 内嵌
    neighbors）。邻居只"带出"不"提分"——公式与排序不受影响（#7）。
    """
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    docs = [tokenize(doc_text(m)) for m in candidates]
    rels = bm25_scores(q_tokens, docs)
    hits: list[dict[str, Any]] = []
    for mem, rel in zip(candidates, rels):
        if rel <= 0:
            continue
        sim = normalized_similarity(rel, len(q_tokens))
        rec = recency_score(mem, now)
        score = final_score(sim, mem.confidence, rec, mem.type)
        hits.append(
            {
                "id": mem.id,
                "score": round(score, 4),
                "similarity": round(sim, 4),
                "confidence": mem.confidence,
                "uses": mem.uses,
                "type": mem.type,
                "ns": mem.ns,
                "source": mem.source,
                "content": mem.content,
            }
        )
    hits.sort(key=lambda h: -h["score"])
    top = hits[:top_k]
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
