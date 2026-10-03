"""scoring.rank 单元测试（TDD：先于实现编写）。

rank 是排序管线的单一定义点：tokenize → BM25 → 归一化 → 新近 → 合分 → 排序 → 结果形状。
这些测试编码"为什么重要"：归一化必须除以同一套 tokenize 的 token 数（原始 BM25 会超 1）、
坏日期不得炸排序、结果形状只有一处定义、复合排序的每个因子方向都被钉住。
"""

from __future__ import annotations

import datetime as dt

import pytest

from compound_memory.model import Memory
from compound_memory.scoring import (
    W_CONF,
    W_RECENCY,
    W_SIM,
    W_TYPE,
    TYPE_WEIGHT,
    age_days,
    dup_similarity_matrix,
    rank,
    recency_age,
)


TODAY = dt.date(2026, 10, 1)


def _days_ago(n: int) -> str:
    return (TODAY - dt.timedelta(days=n)).isoformat()


def make_mem(
    seq: int,
    content: str,
    *,
    type: str = "episode",
    confidence: float = 0.5,
    created: str | None = None,
    last_used: str | None = None,
    uses: int = 0,
) -> Memory:
    return Memory(
        id=f"20260101_{seq:06d}",
        ns="_shared",
        type=type,
        source="agent-a",
        created=created or _days_ago(0),
        content=content,
        confidence=confidence,
        uses=uses,
        last_used=last_used,
    )


NOW = TODAY


class TestRankPipeline:
    def test_empty_query_returns_empty(self):
        assert rank("   ", [make_mem(1, "python gil")], now=NOW) == []

    def test_no_token_match_returns_empty(self):
        hits = rank("kubernetes helm", [make_mem(1, "redis persistence")], now=NOW)
        assert hits == []

    def test_hit_shape_is_single_definition_point(self):
        """搜索结果的形状由 rank 一处定义——MCP 工具与 CLI 只认这里。"""
        mem = make_mem(1, "python gil", confidence=0.7, uses=3)
        (hit,) = rank("python", [mem], now=NOW)
        assert set(hit) == {"id", "score", "similarity", "confidence", "uses", "type", "ns", "source", "content"}
        assert hit["id"] == mem.id
        assert hit["uses"] == 3
        assert hit["confidence"] == 0.7

    def test_similarity_clamped_via_query_token_count(self):
        """归一化必须除以 query 的 token 数：原始 BM25 远超 1，直接用会让合分爆表。"""
        big = make_mem(1, "python " * 100)
        others = [make_mem(2, "rust go"), make_mem(3, "rust go")]
        (hit,) = rank("python", [big, *others], now=NOW)
        assert hit["similarity"] == 1.0

    def test_top_k_cutoff(self):
        mems = [make_mem(i, "nginx buffer") for i in range(3)]
        assert len(rank("nginx", mems, now=NOW, top_k=2)) == 2


class TestCompositeOrdering:
    def test_confidence_breaks_similarity_ties(self):
        low = make_mem(1, "redis persistence", confidence=0.5)
        high = make_mem(2, "redis persistence", confidence=0.9)
        hits = rank("redis", [low, high], now=NOW)
        assert [h["id"] for h in hits] == [high.id, low.id]

    def test_recency_reference_is_last_used_not_created(self):
        """新近基准（CONTEXT.md: recency reference）：last_used 优先于 created。
        排序与衰减共用这一语义——否则会出现"排序眼里很新、衰减眼里已死"的分叉。"""
        fresh = make_mem(1, "vercel timeout", created=_days_ago(100), last_used=_days_ago(1))
        stale = make_mem(2, "vercel timeout", created=_days_ago(100))
        hits = rank("vercel", [stale, fresh], now=NOW)
        assert [h["id"] for h in hits] == [fresh.id, stale.id]

    def test_type_weight_breaks_full_ties(self):
        """同日期同置信度同相似度：skill(1.0) 压过 episode(0.5)。
        两条 created 都是今天 ⇒ 新近度同为 1.0，差异只能来自类型权重。"""
        skill = make_mem(1, "docker prune", type="skill")
        episode = make_mem(2, "docker prune", type="episode")
        hits = rank("docker", [episode, skill], now=NOW)
        assert [h["id"] for h in hits] == [skill.id, episode.id]


class TestDegradedDates:
    def test_unparseable_date_scores_neutral_recency(self):
        """坏日期 ⇒ 新近项按中性值 0.5 计（底座语义：不奖励也不惩罚），排序不炸。"""
        mem = make_mem(1, "python tutorial", last_used="not-a-date")
        (hit,) = rank("python", [mem], now=NOW)
        assert hit["score"] == pytest.approx(
            W_SIM * hit["similarity"] + W_CONF * 0.5 + W_RECENCY * 0.5 + W_TYPE * TYPE_WEIGHT["episode"],
            abs=1e-3,
        )


class TestRRFFusion:
    """双路 RRF 融合（vec-spike S5 形态）：RRF rank 定主序，先验只做 ε 内 tie-break。"""

    def test_vector_only_candidates_enter_results(self):
        """词面零命中但向量召回的候选必须能进结果——扩候选是向量路的全部意义。"""
        mem = make_mem(1, "redis persistence")
        hits = rank("缓冲区设置", [mem], now=NOW, vec_sims={mem.id: 0.9})
        assert [h["id"] for h in hits] == [mem.id]
        assert hits[0]["similarity"] == pytest.approx(0.5, abs=0.01)  # 单路 rank1 的 RRF norm

    def test_full_hit_on_both_channels_normalizes_to_one(self):
        """两路都 rank1 ⇒ 融合相似度归一到 1.0（结果形状语义不漂移）。"""
        mem = make_mem(1, "python tutorial")
        (hit,) = rank("python 学习", [mem], now=NOW, vec_sims={mem.id: 0.8})
        assert hit["similarity"] == pytest.approx(1.0, abs=1e-6)

    def test_prior_never_overturns_rank_gap(self):
        """核心设计约束：conf 高 0.5 抵不过 RRF rank1 vs rank2 的差距——先验翻不了主序。"""
        strong = make_mem(1, "nginx buffer", confidence=0.5)
        weak = make_mem(2, "nginx proxy", confidence=1.0)
        hits = rank(
            "nginx buffer",  # strong 词面满命中 rank1，weak 只共享 nginx 排 rank2
            [weak, strong],
            now=NOW,
            vec_sims={strong.id: 0.9, weak.id: 0.3},
        )
        # strong：词面 rank1 + 向量 rank1；weak：两路 rank2——RRF 主序必压过 conf 马太效应
        assert [h["id"] for h in hits] == [strong.id, weak.id]

    def test_prior_breaks_true_ties(self):
        """RRF 完全同分（词面与向量 rank 交叉对称）时，先验 tie-break 决定先后。"""
        low_conf = make_mem(1, "alpha beta", confidence=0.5)
        high_conf = make_mem(2, "gamma delta", confidence=0.9)
        hits = rank(
            "alpha gamma",  # 两条各命中一词、dl/tf 对称 ⇒ 词面 rank 按 candidates 序
            [low_conf, high_conf],
            now=NOW,
            vec_sims={low_conf.id: 0.8, high_conf.id: 0.9},  # 向量 rank 反转 ⇒ fused 打平
        )
        assert [h["id"] for h in hits] == [high_conf.id, low_conf.id]

    def test_single_channel_degrades_to_lexical_order(self):
        """vec_sims=None 的单路退路：无词面命中 ⇒ 空结果（历史行为零回归）。"""
        mem = make_mem(1, "redis persistence")
        assert rank("缓冲区设置", [mem], now=NOW, vec_sims=None) == []


class TestDistillDupMatrix:
    def test_similar_docs_flag_each_other_unrelated_score_zero(self):
        """蒸馏疑似重复信号的纯数学半边：近似文本互得高分、无关文本零分。
        不需要 store/文件树——阈值标注（possible_dup_of）是 distill-plan 的策略，数学归 scoring。"""
        docs = [
            "compound-memory 蒸馏管线把候选清单交给 agent 判断",
            "compound-memory 蒸馏管线把候选清单交给调用方判断",
            "Kubernetes Pod 亲和性配置",
        ]
        matrix = dup_similarity_matrix(docs)
        assert len(matrix) == 3 and all(len(row) == 3 for row in matrix)
        assert all(row[i] > 0 for i, row in enumerate(matrix))  # 自查自必得分
        assert matrix[0][2] == 0.0 and matrix[2][0] == 0.0  # 无关文本零匹配
        assert matrix[0][1] > matrix[0][2]  # 近似文本高于无关文本（BM25 非对称，只比相对大小）


class TestRecencyAge:
    def test_age_days_shared_parse_helper(self):
        """stats 的 last_used 窗口与 recency_age 共用同一解析降级——坏日期语义不会分叉。"""
        assert age_days("2026-09-21", TODAY) == 10
        assert age_days("not-a-date", TODAY) is None

    def test_last_used_wins_over_created(self):
        mem = make_mem(1, "x", created="2020-01-01", last_used="2026-09-30")
        assert recency_age(mem, dt.date(2026, 10, 1)) == 1

    def test_falls_back_to_created(self):
        mem = make_mem(1, "x", created="2026-09-21")
        assert recency_age(mem, dt.date(2026, 10, 1)) == 10

    def test_bad_date_returns_none(self):
        """坏日期交出 None（决策在消费方：rank 记 0 分、decay 跳过），绝不在缝上崩。"""
        mem = make_mem(1, "x", last_used="not-a-date")
        assert recency_age(mem, dt.date(2026, 10, 1)) is None
