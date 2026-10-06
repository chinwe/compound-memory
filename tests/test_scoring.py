"""scoring.rank 单元测试（TDD：先于实现编写）。

rank 是排序管线的单一定义点：tokenize → BM25 → 归一化 → 新近 → 合分 → 排序 → 结果形状。
这些测试编码"为什么重要"：归一化必须除以同一套 tokenize 的 token 数（原始 BM25 会超 1）、
坏日期不得炸排序、结果形状只有一处定义、复合排序的每个因子方向都被钉住。
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter

import pytest

from compound_memory.model import Memory
from compound_memory.scoring import (
    PRIOR_EPSILON,
    RRF_K,
    W_CONF,
    W_RECENCY,
    W_SIM,
    W_TYPE,
    TYPE_WEIGHT,
    DocStats,
    age_days,
    bm25_scores,
    bm25_scores_from_stats,
    doc_stats_from_tokens,
    doc_text,
    dup_similarity_matrix,
    expired_by_date,
    is_expired,
    rank,
    recency_age,
    tokenize,
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
    valid_until: str | None = None,
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
        valid_until=valid_until,
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


class TestRankExplain:
    """opt-in 排序分量透出（#44，ADR-0008 展示边界）：rank 的 emit 单点在
    explain=True 时附分量对象——单路给 final_score 四分量折算、双路给 RRF 分 +
    先验 ε 折算，sum(terms) 与最终 score 在 epsilon 内对账（票面验收）；
    缺省（explain=False）输出形状逐位不变（默认 hit 键集一条不得增删）。
    """

    BASE_KEYS = {"id", "score", "similarity", "confidence", "uses", "type", "ns", "source", "content"}

    def test_explain_off_leaves_shape_unchanged(self):
        mem = make_mem(1, "python gil", confidence=0.7)
        (hit,) = rank("python", [mem], now=NOW)
        assert set(hit) == self.BASE_KEYS

    def test_opt_in_adds_only_explain_key(self):
        mem = make_mem(1, "python gil", confidence=0.7)
        (hit,) = rank("python", [mem], now=NOW, explain=True)
        assert set(hit) - self.BASE_KEYS == {"explain"}

    def test_explain_true_changes_nothing_else(self):
        """explain=True 除新增 explain 键外逐位等于缺省输出——单路与双路都钉。"""
        cands = [make_mem(1, "redis persistence aof"), make_mem(2, "redis 淘汰策略")]
        for extra in ({}, {"vec_sims": {cands[0].id: 0.9, cands[1].id: 0.4}}):
            base = rank("redis", cands, now=NOW, top_k=5, **extra)
            opt = rank("redis", cands, now=NOW, top_k=5, explain=True, **extra)
            assert [{k: v for k, v in h.items() if k != "explain"} for h in opt] == base

    def test_linear_path_terms_reconcile(self):
        """单路（纯词面）：final_score 的四个折算项之和 == score（epsilon 内）。"""
        mem = make_mem(1, "python gil", type="insight", confidence=0.7, created=_days_ago(10))
        (hit,) = rank("python", [mem], now=NOW, explain=True)
        e = hit["explain"]
        assert e["path"] == "linear" and e["channel"] == "lexical"
        assert e["lexical_rank"] == 1 and e["vector_rank"] is None and e["rrf_score"] is None
        assert e["similarity"] == hit["similarity"]
        rec = 0.5 + 0.5 * math.exp(-10 / 90)  # insight τ=90
        assert e["recency_score"] == pytest.approx(rec, abs=1e-4)
        assert e["type_weight"] == TYPE_WEIGHT["insight"]
        assert sum(e["terms"].values()) == pytest.approx(hit["score"], abs=1e-3)
        assert e["terms"]["similarity"] == pytest.approx(W_SIM * hit["similarity"], abs=1e-3)
        assert e["terms"]["confidence"] == pytest.approx(W_CONF * 0.7, abs=1e-3)
        assert e["terms"]["recency"] == pytest.approx(W_RECENCY * rec, abs=1e-3)
        assert e["terms"]["type"] == pytest.approx(W_TYPE * TYPE_WEIGHT["insight"], abs=1e-3)

    def test_rrf_path_terms_reconcile(self):
        """双路 RRF：similarity 槽 = 归一化融合分，先验三分量按 ε 折算——总和 == score。"""
        strong = make_mem(1, "nginx buffer", type="fact", confidence=0.9)
        weak = make_mem(2, "nginx proxy", type="episode", confidence=0.2)
        hits = rank("nginx buffer", [weak, strong], now=NOW, vec_sims={strong.id: 0.9, weak.id: 0.3}, explain=True)
        by_id = {h["id"]: h for h in hits}
        e = by_id[strong.id]["explain"]
        assert e["path"] == "rrf" and e["channel"] == "both"
        assert e["lexical_rank"] == 1 and e["vector_rank"] == 1
        assert e["rrf_score"] == pytest.approx(2.0 / (RRF_K + 1), abs=1e-3)
        assert e["similarity"] == pytest.approx(1.0, abs=1e-4)  # 双路满命中归一
        prior = 0.5 * 0.9 + 0.3 * 1.0 + 0.2 * TYPE_WEIGHT["fact"]  # created 今天 ⇒ rec_n=1
        assert sum(e["terms"].values()) == pytest.approx(by_id[strong.id]["score"], abs=1e-3)
        assert sum(e["terms"].values()) == pytest.approx(1.0 + PRIOR_EPSILON * prior, abs=1e-3)
        assert e["terms"]["confidence"] == pytest.approx(PRIOR_EPSILON * 0.5 * 0.9, abs=1e-3)
        assert e["terms"]["type"] == pytest.approx(PRIOR_EPSILON * 0.2 * TYPE_WEIGHT["fact"], abs=1e-3)
        w = by_id[weak.id]["explain"]
        assert w["lexical_rank"] == 2 and w["vector_rank"] == 2 and w["channel"] == "both"
        assert w["rrf_score"] == pytest.approx(2.0 / (RRF_K + 2), abs=1e-3)

    def test_channel_labels_single_channel_presence(self):
        """通道标注如实：仅词面 / 仅向量 / 双通道——向量独有召回（词面零命中）
        必须可辨识，否则「这条为什么进结果」解释不了。"""
        lex_only = make_mem(1, "nginx buffer log")
        vec_only = make_mem(2, "redis persistence")
        hits = rank(
            "nginx buffer",  # vec_only 词面零命中，靠向量召回进入
            [lex_only, vec_only],
            now=NOW,
            vec_sims={vec_only.id: 0.9},
            explain=True,
        )
        by_id = {h["id"]: h for h in hits}
        assert by_id[lex_only.id]["explain"]["channel"] == "lexical"
        assert by_id[lex_only.id]["explain"]["lexical_rank"] == 1
        assert by_id[lex_only.id]["explain"]["vector_rank"] is None
        assert by_id[vec_only.id]["explain"]["channel"] == "vector"
        assert by_id[vec_only.id]["explain"]["lexical_rank"] is None
        assert by_id[vec_only.id]["explain"]["vector_rank"] == 1


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


class TestExpired:
    """valid_until 过期判定（Zep 式时态模型的单一定义点）。"""

    def test_valid_until_today_still_valid(self):
        """valid_until 语义是"有效期至"（含当日）：当天仍可信，次日起检索排除。"""
        mem = make_mem(1, "redis persistence", valid_until=TODAY.isoformat())
        assert is_expired(mem, TODAY) is False

    def test_expires_the_day_after_valid_until(self):
        mem = make_mem(1, "redis persistence", valid_until="2026-09-30")
        assert is_expired(mem, TODAY) is True

    def test_future_valid_until_not_expired(self):
        mem = make_mem(1, "x", valid_until="2027-01-01")
        assert is_expired(mem, TODAY) is False

    def test_missing_or_bad_valid_until_not_expired(self):
        """坏数据不冒充过期（与 _within_days 同哲学）：宁可带进检索，不因坏日期静默吞记忆。"""
        assert is_expired(make_mem(1, "x"), TODAY) is False
        assert is_expired(make_mem(2, "x", valid_until="not-a-date"), TODAY) is False


class TestTokenStatsSource:
    """候选 token 统计源（#41）：BM25 的 token 输入从「即时 tokenize」抽象为
    「可注入的 DocStats」，缓存路径（Index per-doc 频表）与词面路径必须
    数学逐位一致——这是「检索结果逐位不变」红线的单元级表达。"""

    DOCS = [
        "redis 缓存 淘汰策略 persistence aof rdb",
        "nginx 反向代理 buffer upstream 限流",
        "docker 镜像 prune 网络 bridge compose 部署",
        "redis 持久化 persistence 淘汰策略 淘汰策略 缓存",
    ]

    def _stats_of(self, text: str) -> DocStats:
        toks = tokenize(text)
        return DocStats(tf=dict(Counter(toks)), dl=len(toks))

    def test_bm25_stats_path_matches_token_list_bit_for_bit(self):
        """频表路径与列表路径同分：tf/dl/df/avgdl 的每个输入都来自同一份
        tokenize 结果，只是组织形式不同（缓存免 parse 的前提）。"""
        q = tokenize("redis 淘汰策略")
        docs_tokens = [tokenize(d) for d in self.DOCS]
        stats = [self._stats_of(d) for d in self.DOCS]
        assert bm25_scores(q, docs_tokens) == bm25_scores_from_stats(q, stats)

    def test_rank_doc_stats_matches_tokenize_path(self):
        """rank 带不带 doc_stats 输出逐位一致（纯词面与 RRF 双路都钉）——
        缓存路径改变的是 token 来源，不是排序管线。"""
        cands = [make_mem(i, d) for i, d in enumerate(self.DOCS, 1)]
        stats = [self._stats_of(doc_text(m)) for m in cands]
        q = "redis 淘汰策略"
        assert rank(q, cands, now=NOW, top_k=4, doc_stats=stats) == rank(q, cands, now=NOW, top_k=4)
        vec_sims = {cands[0].id: 0.9, cands[3].id: 0.4}
        assert rank(q, cands, now=NOW, top_k=4, vec_sims=vec_sims, doc_stats=stats) == rank(
            q, cands, now=NOW, top_k=4, vec_sims=vec_sims
        )

    def test_rank_doc_stats_partial_none_falls_back_to_tokenize(self):
        """对齐列表中的 None 位（缓存无条目的候选）回退 tokenize 该文档——
        混合来源（词法缓存 ∪ 向量独有召回）的正确性。"""
        cands = [make_mem(1, self.DOCS[0]), make_mem(2, self.DOCS[1])]
        mixed = [self._stats_of(self.DOCS[0]), None]
        assert rank("redis 缓存", cands, now=NOW, doc_stats=mixed) == rank("redis 缓存", cands, now=NOW)

    def test_content_loader_supplies_emit_content(self):
        """content_loader（缓存候选的正文现 parse 缝）：命中条目的 content
        来自 loader，未命中路径保持 mem.content。"""
        cands = [make_mem(1, "lazy content marker")]
        stats = [self._stats_of("lazy content marker")]
        hits = rank(
            "lazy content",
            cands,
            now=NOW,
            doc_stats=stats,
            content_loader=lambda mid: f"loaded:{mid}",
        )
        assert hits[0]["content"] == f"loaded:{cands[0].id}"
        # 缺省不传 loader：正文仍取候选自身（词面路径现状）
        assert rank("lazy content", cands, now=NOW, doc_stats=stats)[0]["content"] == "lazy content marker"


class TestExpiredByDate:
    """valid_until 原语（#41）：字符串直判与 is_expired 的 Memory 判定同源——
    词法缓存的 per-doc 条目（无 Memory 形态）复用过期语义，勿各算各的。"""

    @pytest.mark.parametrize(
        "valid_until,expected",
        [(None, False), ("", False), ("2026-10-01", False), ("2026-09-30", True), ("2027-01-01", False), ("bad", False)],
    )
    def test_matches_is_expired_semantics(self, valid_until, expected):
        mem = make_mem(1, "x", valid_until=valid_until)
        assert expired_by_date(valid_until, TODAY) is expected
        assert is_expired(mem, TODAY) is expected

    def test_content_loader_called_only_for_returned_hits(self):
        """loader 只许在 top_k 切片后调用：emit 阶段每个正分候选都会经过，
        宽查询若在 emit 里现 parse 正文，免 parse 的候选缓存会被全数吃回
        （perf-bench #41 实测：万条 broad 8526 次 emit 级 parse）。"""
        cands = [make_mem(i, f"redis shared filler {i} redis") for i in range(1, 21)]
        stats = [doc_stats_from_tokens(tokenize(doc_text(m))) for m in cands]
        calls: list[str] = []
        hits = rank(
            "redis filler",
            cands,
            now=NOW,
            top_k=5,
            doc_stats=stats,
            content_loader=lambda mid: calls.append(mid) or f"c:{mid}",
        )
        assert len(calls) == 5 == len(hits)
        assert all(h["content"] == f"c:{h['id']}" for h in hits)
