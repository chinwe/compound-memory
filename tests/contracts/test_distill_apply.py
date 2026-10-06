"""distill_apply 动词契约（store 层五要素 characterization）。

契约出处：#26 P5（失败语义：missing ⇒ found:False 零操作零提交；跨 ns ⇒
ValueError 整体拒绝；源去重保序；批尾单 commit、消息含产物 id 与源清单）。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, PRIVATE_NS, commit_count, last_message, matches


def _seed_sources(store: MemoryStore, n: int = 2) -> list[dict]:
    out = []
    for i in range(n):
        mem = store.write(f"contract apply source {i}", type="episode", source="agent-a")
        store.feedback(mem["id"], "agent-a")  # 过蒸馏活性门（与本契约无关，仅播种）
        out.append(mem)
    return out


class TestInputEquivalence:
    def test_missing_source_is_zero_op_envelope(self, store: MemoryStore):
        """P5：任一源不存在 ⇒ {"found": False, "missing": [...]}，零操作零提交，
        存活的源保持活动态。"""
        src = _seed_sources(store, 1)[0]
        before = commit_count(store)
        out = store.distill_apply(
            "product", type="insight", source="agent-a", source_ids=[src["id"], "nope"]
        )
        assert out == {"found": False, "missing": ["nope"]}
        assert commit_count(store) == before
        assert store.get(src["id"])["archived"] is False

    def test_cross_ns_source_rejected_atomically(self, store: MemoryStore):
        """P5：蒸馏不跨 ns（正文泄漏通道）——ValueError 整体拒绝，零提交。"""
        priv = store.write("contract apply priv src", type="episode", source=OWNER, ns=PRIVATE_NS)
        before = commit_count(store)
        with pytest.raises(ValueError, match="must live in target ns"):
            store.distill_apply("product", type="insight", source=OWNER, source_ids=[priv["id"]])
        assert commit_count(store) == before
        assert store.get(priv["id"], reader=OWNER)["archived"] is False

    def test_duplicate_sources_deduped_preserving_order(self, store: MemoryStore):
        """P5：源去重保序——重复源只归档一次、消息只列一次。"""
        a, b = _seed_sources(store, 2)
        out = store.distill_apply(
            "product", type="insight", source="agent-a", source_ids=[a["id"], b["id"], a["id"]]
        )
        assert out["archived_sources"] == [a["id"], b["id"]]

    def test_invalid_product_type_rejected_inside_batch(self, store: MemoryStore):
        """逐条校验写穿：批内 write 的非法 type 照样 ValueError，且零提交
        （校验失败发生在任何落库之前）。"""
        src = _seed_sources(store, 1)[0]
        before = commit_count(store)
        with pytest.raises(ValueError, match="type must be one of"):
            store.distill_apply("product", type="bogus", source="agent-a", source_ids=[src["id"]])
        assert commit_count(store) == before
        assert store.get(src["id"])["archived"] is False


class TestPermissionMatrix:
    def test_private_apply_by_owner_ok(self, store: MemoryStore):
        """同 ns 蒸馏按属主放行：私有源蒸进私有 ns（write 门禁经 owner 身份）。"""
        priv = store.write("contract apply priv2", type="episode", source=OWNER, ns=PRIVATE_NS)
        out = store.distill_apply(
            "private product", type="insight", source=OWNER, source_ids=[priv["id"]], ns=PRIVATE_NS
        )
        assert out["found"] is True and out["ns"] == PRIVATE_NS

    def test_private_apply_by_foreign_rejected_atomically(self, store: MemoryStore):
        """外来 agent 把产物写进私有 ns：PermissionError（write 侧门禁），零提交。"""
        priv = store.write("contract apply priv3", type="episode", source=OWNER, ns=PRIVATE_NS)
        before = commit_count(store)
        with pytest.raises(PermissionError, match="private"):
            store.distill_apply(
                "forged product", type="insight", source=FOREIGN, source_ids=[priv["id"]], ns=PRIVATE_NS
            )
        assert commit_count(store) == before
        assert store.get(priv["id"], reader=OWNER)["archived"] is False


class TestSideEffects:
    def test_single_commit_with_template_message(self, store: MemoryStore):
        """P5：产物写入 + 源归档收进恰好一次 commit，消息含产物 id 与源清单。"""
        a, b = _seed_sources(store, 2)
        before = commit_count(store)
        out = store.distill_apply(
            "merged product", type="insight", source="agent-zcode", source_ids=[a["id"], b["id"]]
        )
        assert commit_count(store) == before + 1
        m = matches("distill_apply", last_message(store))
        assert m["id"] == out["id"]
        assert m["sources"] == f"{a['id']}, {b['id']}"

    def test_product_carries_origin_and_source_links(self, store: MemoryStore):
        """产物形状：origin=distillation、links 溯源全部源、可检索。"""
        a, b = _seed_sources(store, 2)
        out = store.distill_apply("merged product", type="insight", source="agent-a", source_ids=[a["id"], b["id"]])
        assert out["origin"] == "distillation"
        assert out["links"] == [a["id"], b["id"]]
        assert [h["id"] for h in store.search("merged product")] == [out["id"]]

    def test_sources_archived_to_archive_path(self, store: MemoryStore):
        src = _seed_sources(store, 1)[0]
        store.distill_apply("merged product", type="insight", source="agent-a", source_ids=[src["id"]])
        assert (store.archive_root / "_shared" / "episode" / f"{src['id']}.md").exists()
        assert not (store.ns_root / "_shared" / "episode" / f"{src['id']}.md").exists()
        assert store.search("contract apply source") == []  # 归档必不在索引

    def test_already_archived_source_skipped_not_moved(self, store: MemoryStore):
        """已是归档态的源不重复搬运（幂等），但仍计入 archived_sources 与消息。"""
        src = store.write("contract apply archived src", type="episode", source="agent-a", created="2026-01-01")
        assert src["id"] in store.decay_sweep()
        out = store.distill_apply("merged product", type="insight", source="agent-a", source_ids=[src["id"]])
        assert out["archived_sources"] == [src["id"]]
        assert (store.archive_root / "_shared" / "episode" / f"{src['id']}.md").exists()
