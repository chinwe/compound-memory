"""explain 契约（#44，ADR-0007/0008 展示边界）：opt-in 排序分量 + 按 id 证据视图。

spec #52 Explain 裁决的两条载体：① search 的 opt-in explain 参数（CLI --explain /
MCP explain 参数）——缺省返回形状逐位不变，本文件只钉 opt-in 附加形状（BASE_HIT_KEYS
仅作差集基准引用，不改 test_search 冻结的 10 键断言）；② store.explain 按 id 证据
视图（evidence summary 的按 id 形态）——入口仅 store + CLI，MCP 恰好 5 tool 红线
不动、get 不扩 explain 参数。取数字段 = 证据块 / validated_by / origin（ADR-0008）。
"""

from __future__ import annotations

import json

import pytest

from compound_memory import cli
from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, OWNER_BARE, PRIVATE_NS, commit_count, days_ago

# search hit 的缺省键集（与 test_search.TestResultShape 冻结断言同源；此处仅作
# opt-in 附加形状的差集基准，不构成第二份冻结断言——改默认形状仍以 test_search 为准）
BASE_HIT_KEYS = {"id", "score", "similarity", "confidence", "uses", "type", "ns", "source", "content", "neighbors"}
# opt-in 附加键（#44）：排序分量对象 + 证据摘要行
EXPLAIN_EXTRA_KEYS = {"explain", "evidence"}

EXPLAIN_VIEW_KEYS = {
    "found", "id", "ns", "type", "source", "archived", "confidence", "uses",
    "origin", "derived", "validated_by", "evidence",
}
EVIDENCE_VIEW_KEYS = {"success_count", "failure_count", "contradiction_count", "last_verified", "recent"}


class TestSearchExplainOptIn:
    def test_default_hits_carry_no_explanation(self, store: MemoryStore):
        """缺省（不传 explain）返回形状逐位不变——红线：一条键都不得增删。"""
        store.write("explain default shape marker", type="fact", source="agent-a")
        (hit,) = store.search("default shape marker")
        assert set(hit) == BASE_HIT_KEYS

    def test_explain_false_explicit_is_default_shaped(self, store: MemoryStore):
        """显式 explain=False 与缺省同形（bit-identical 的显式钉法）。"""
        store.write("explain off marker", type="fact", source="agent-a")
        (hit,) = store.search("explain off marker", explain=False)
        assert set(hit) == BASE_HIT_KEYS

    def test_opt_in_attaches_components_and_evidence_line(self, store: MemoryStore):
        mem = store.write("explain optin shape marker", type="fact", source="agent-a")
        (hit,) = store.search("optin shape marker", explain=True)
        assert set(hit) == BASE_HIT_KEYS | EXPLAIN_EXTRA_KEYS
        assert hit["id"] == mem["id"]
        # store fixture 无 embedder ⇒ 纯词面单路
        assert hit["explain"]["path"] == "linear"
        assert hit["explain"]["channel"] == "lexical"
        ev = hit["evidence"]
        assert set(ev) == {"origin", "success_count", "failure_count", "contradiction_count", "last_verified"}
        assert ev["origin"] is None
        assert ev["success_count"] == 0 and ev["last_verified"] is None

    def test_rrf_path_reconciles_through_search(self, vec_store: MemoryStore):
        """对账验收（票面 Acceptance）：经 store 动词、RRF 双路下 sum(terms) == score
        （epsilon 内）；可见性断言显式放大 top_k——防截断伪装成丢更新。"""
        vec_store.write("rrf reconcile docker bridge notes", type="fact", source="agent-a")
        hits = vec_store.search("rrf reconcile docker bridge", top_k=10, explain=True)
        assert hits
        for hit in hits:
            assert hit["explain"]["path"] == "rrf"
            assert sum(hit["explain"]["terms"].values()) == pytest.approx(hit["score"], abs=1e-3)

    def test_evidence_summary_tracks_feedback(self, store: MemoryStore):
        """证据摘要行随 feedback 折算如实（ADR-0007）：success 后计数与 last_verified
        取固定 clock 日期——证据在查询时间可见（spec #52 user story 25）。"""
        mem = store.write("evidence summary marker", type="fact", source=FOREIGN)
        store.feedback(mem["id"], OWNER)
        (hit,) = store.search("evidence summary marker", explain=True)
        ev = hit["evidence"]
        assert ev["success_count"] == 1
        assert ev["failure_count"] == 0 and ev["contradiction_count"] == 0
        assert ev["last_verified"] == days_ago(0)

    def test_no_full_text_duplication_in_explain_output(self, store: MemoryStore):
        """紧凑性（票面 Acceptance）：explain/evidence 附加对象不重复正文，
        值均有界（分量是数字与短标签、摘要是计数与日期）。"""
        body = "compactness probe " + "filler " * 50
        store.write(body, type="fact", source="agent-a")
        (hit,) = store.search("compactness probe", explain=True)
        blob = json.dumps({"explain": hit["explain"], "evidence": hit["evidence"]})
        assert "filler" not in blob


class TestExplainVerb:
    def test_missing_id_returns_found_envelope(self, store: MemoryStore):
        """按 id 动词恒含 found 键；非法 id 等价不存在（find 容错覆盖）。"""
        assert store.explain("nope") == {"found": False}

    def test_shape_pinned(self, store: MemoryStore):
        mem = store.write("explain verb shape", type="fact", source="agent-a")
        out = store.explain(mem["id"])
        assert set(out) == EXPLAIN_VIEW_KEYS
        assert set(out["evidence"]) == EVIDENCE_VIEW_KEYS
        assert out["found"] is True and out["id"] == mem["id"]
        assert out["confidence"] == 0.5
        assert out["origin"] is None and out["derived"] is False
        assert out["validated_by"] == []
        assert out["evidence"]["recent"] == []

    def test_read_only_no_commit(self, store: MemoryStore):
        mem = store.write("explain read only", type="fact", source="agent-a")
        before = commit_count(store)
        store.explain(mem["id"])
        assert commit_count(store) == before

    def test_private_ns_owner_gate(self, store: MemoryStore):
        """凡返回记忆元数据的新入口先过身份门（AGENTS.md 不变量）：私有 ns
        仅属主可读——缺身份/外来身份都 PermissionError，全称/短名等价归一。"""
        priv = store.write("explain private gate", type="fact", source=OWNER, ns=PRIVATE_NS)
        with pytest.raises(PermissionError):
            store.explain(priv["id"])
        with pytest.raises(PermissionError):
            store.explain(priv["id"], reader=FOREIGN)
        out = store.explain(priv["id"], reader=OWNER_BARE)
        assert out["found"] is True

    def test_lazy_migration_view_without_block(self, store: MemoryStore):
        """无块旧记忆（ADR-0007 惰性迁移）：证据视图读为 success_count=uses，
        读路径不落块（首次 feedback 才写）——explain 是纯视图，不做迁移写。"""
        mem = store.write("lazy migration probe", type="fact", source="agent-a")
        got = store.find(mem["id"])
        assert got is not None
        got.uses = 2
        store._save(got)  # evidence=None 不落盘 ⇒ 构造存量无块形态
        out = store.explain(mem["id"])
        assert out["evidence"]["success_count"] == 2
        assert out["evidence"]["failure_count"] == 0 and out["evidence"]["contradiction_count"] == 0
        again = store.find(mem["id"])
        assert again is not None and again.evidence is None

    def test_feedback_block_visible_with_validation_detail(self, store: MemoryStore):
        mem = store.write("feedback block probe", type="fact", source=FOREIGN)
        store.feedback(mem["id"], OWNER)
        out = store.explain(mem["id"])
        assert out["evidence"]["success_count"] == 1
        assert out["evidence"]["last_verified"] == days_ago(0)
        assert out["validated_by"] == [OWNER]  # 跨宿主验证明细（ADR-0008 语义字段）

    def test_distill_product_derived_zero_start(self, store: MemoryStore):
        """蒸馏产物（ADR-0008）：derived=True + origin=distillation + 证据显式零起点
        ——「不继承源证据」在 explain 视图可辨。"""
        s1 = store.write("distill explain source one", type="episode", source="agent-a")
        s2 = store.write("distill explain source two", type="episode", source="agent-a")
        product = store.distill_apply("merged explain insight", "insight", "agent-a", [s1["id"], s2["id"]])
        out = store.explain(product["id"])
        assert out["origin"] == "distillation" and out["derived"] is True
        ev = out["evidence"]
        assert ev["success_count"] == 0 and ev["failure_count"] == 0 and ev["contradiction_count"] == 0
        assert out["validated_by"] == []


class TestCliExplainAdapter:
    """CLI 是薄 adapter：只钉输出形状（explain 视图 / search --explain 附加键），
    语义不重测。"""

    def test_cli_explain_emits_view_json(self, store: MemoryStore, capsys: pytest.CaptureFixture[str]):
        mem = store.write("cli explain probe", type="fact", source="agent-a")
        assert cli.main(["--root", str(store.root), "explain", mem["id"]]) == 0
        out = json.loads(capsys.readouterr().out)
        assert set(out) == EXPLAIN_VIEW_KEYS

    def test_cli_search_explain_flag(self, store: MemoryStore, capsys: pytest.CaptureFixture[str]):
        store.write("cli search explain probe", type="fact", source="agent-a")
        assert cli.main(["--root", str(store.root), "search", "search explain probe", "--explain"]) == 0
        (hit,) = json.loads(capsys.readouterr().out)
        assert set(hit) == BASE_HIT_KEYS | EXPLAIN_EXTRA_KEYS

    def test_cli_explain_missing_id_exit_zero(self, store: MemoryStore, capsys: pytest.CaptureFixture[str]):
        """missing 走 found 信封（正常返回，非调用方错误——不 exit 2）。"""
        assert cli.main(["--root", str(store.root), "explain", "nope"]) == 0
        assert json.loads(capsys.readouterr().out) == {"found": False}
