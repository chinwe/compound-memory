"""Scoring: tokenization, lexical similarity (BM25), recency decay, final score.

检索得分 = 0.45·相似度 + 0.25·置信度 + 0.20·新近度(e^(-Δt/τ)) + 0.10·类型权重

rank 是排序管线的单一定义点：调用方传入原始 query 与候选记忆，
tokenize → BM25 → 归一化 → 新近 → 合分 → 排序 → 结果形状全部在实现内。
"""

from __future__ import annotations

import datetime as dt
import math
import re
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


def _cjk_bigrams(run: list[str]) -> list[str]:
    if not run:
        return []
    if len(run) == 1:
        return list(run)
    return [run[i] + run[i + 1] for i in range(len(run) - 1)]


def tokenize(text: str) -> list[str]:
    """Lowercase tokenizer: latin/digit words + CJK character bigrams."""
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
    """The searchable text of a memory — single definition point (content + key)."""
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
    """BM25 relevance of each doc for the query. Returns 0.0 when nothing matches."""
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
    """Exponential recency e^(-Δdays/τ); τ by memory type; bad dates ⇒ 0.0."""
    tau = TAU_DAYS.get(mem.type, 90.0)
    age = recency_age(mem, now)
    if age is None:
        return 0.0
    return math.exp(-max(0, age) / tau)


def normalized_similarity(bm25: float, n_query_tokens: int) -> float:
    """Clamp BM25 to [0, 1] by dividing by query length."""
    if n_query_tokens <= 0:
        return 0.0
    return min(1.0, bm25 / n_query_tokens)


def final_score(sim: float, confidence: float, recency: float, mtype: str) -> float:
    return W_SIM * sim + W_CONF * confidence + W_RECENCY * recency + W_TYPE * TYPE_WEIGHT.get(mtype, 0.5)


def rank(query: str, candidates: list[Memory], now: dt.date, top_k: int = 5) -> list[dict[str, Any]]:
    """排序管线：query 与候选记忆进，最终搜索结果出。

    结果 dict 的形状在这里一处定义（id / score / similarity / confidence /
    uses / type / ns / source / content）；空 query 返回 []。
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
    return hits[:top_k]
