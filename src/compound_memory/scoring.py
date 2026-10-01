"""Scoring: tokenization, lexical similarity (BM25), recency decay, final score.

检索得分 = 0.45·相似度 + 0.25·置信度 + 0.20·新近度(e^(-Δt/τ)) + 0.10·类型权重
"""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass

TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]")

TYPE_WEIGHT = {"skill": 1.0, "fact": 0.9, "insight": 0.7, "episode": 0.5}
TAU_DAYS = {"episode": 30.0, "insight": 90.0, "fact": 365.0, "skill": 365.0}

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


@dataclass(frozen=True)
class ScoredHit:
    id: str
    score: float
    similarity: float
    confidence: float
    recency: float


def recency_score(last_used: str | None, created: str, mtype: str, now) -> float:
    """Exponential recency e^(-Δdays/τ); τ by memory type."""
    ref = last_used or created
    tau = TAU_DAYS.get(mtype, 90.0)
    try:
        ref_date = dt.date.fromisoformat(ref)
    except (ValueError, TypeError):
        return 0.0
    days = max(0.0, (now - ref_date).days)
    return math.exp(-days / tau)


def normalized_similarity(bm25: float, n_query_tokens: int) -> float:
    """Clamp BM25 to [0, 1] by dividing by query length."""
    if n_query_tokens <= 0:
        return 0.0
    return min(1.0, bm25 / n_query_tokens)


def final_score(sim: float, confidence: float, recency: float, mtype: str) -> float:
    return W_SIM * sim + W_CONF * confidence + W_RECENCY * recency + W_TYPE * TYPE_WEIGHT.get(mtype, 0.5)
