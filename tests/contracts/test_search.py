"""search 动词契约（store 层五要素 characterization）。

契约出处：#25（读路径不进审计——search 无 git 提交）/ #26（ns 门禁、
双通道缺省检索）/ ADR 0006（search 剔除出事件清单）。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, OWNER_BARE, PRIVATE_NS, commit_count, days_ago


def _seed_shared(store: MemoryStore) -> str:
    return store.write("contract shared search marker", type="fact", source=FOREIGN)["id"]


class TestInputEquivalence:
    def test_blank_query_returns_empty_list(self, store: MemoryStore):
        """空白 query 是合法输入：返回空列表，不抛错（静默空结果是这里的正确语义——
        与非法 ns 必须抛错形成对照）。"""
        assert store.search("   ") == []

    def test_negative_top_k_is_caller_error(self, store: MemoryStore):
        with pytest.raises(ValueError, match="top_k"):
            store.search("x", top_k=-1)

    def test_illegal_ns_is_caller_error_not_silent_empty(self, store: MemoryStore):
        """拼错/非法 ns 必须抛 ValueError：静默空结果会让调用方误判「无相关记忆」。"""
        with pytest.raises(ValueError):
            store.search("marker", ns="shared")
        with pytest.raises(ValueError):
            store.search("marker", ns="agent-../x")

    def test_default_top_k_is_five(self, store: MemoryStore):
        """缺省 top_k=5：6 条可命中记忆只回 5 条；显式放大才见全量。"""
        for i in range(6):
            store.write(f"kxmarker contract default topk item {i}", type="episode", source="agent-a")
        assert len(store.search("kxmarker")) == 5
        assert len(store.search("kxmarker", top_k=10)) == 6


class TestPermissionMatrix:
    def test_shared_ns_reads_ignore_reader(self, store: MemoryStore):
        _seed_shared(store)
        assert [h["id"] for h in store.search("shared search marker", reader=FOREIGN)]
        assert [h["id"] for h in store.search("shared search marker")]

    @pytest.mark.parametrize("reader", [None, FOREIGN], ids=["no-identity", "foreign"])
    def test_private_ns_denied_without_owner(self, store: MemoryStore, reader: str | None):
        """显式私有 ns：缺身份/外来身份都 PermissionError（fail-closed，宁可不读不猜身份）。"""
        store.write("contract private search", type="fact", source=OWNER, ns=PRIVATE_NS)
        with pytest.raises(PermissionError):
            store.search("private search", ns=PRIVATE_NS, reader=reader)

    @pytest.mark.parametrize("reader", [OWNER, OWNER_BARE], ids=["full-form", "bare-form"])
    def test_private_ns_owner_reads(self, store: MemoryStore, reader: str):
        priv = store.write("contract private search", type="fact", source=OWNER, ns=PRIVATE_NS)
        assert [h["id"] for h in store.search("private search", ns=PRIVATE_NS, reader=reader)] == [priv["id"]]


class TestDefaultScope:
    """缺省 ns=None 的双通道语义（#26 已裁）：_shared ∪ 自有私有 ns。"""

    def test_dual_channel_spans_shared_and_own_private(self, store: MemoryStore):
        shared = _seed_shared(store)
        priv = store.write("contract dual channel private", type="fact", source=OWNER, ns=PRIVATE_NS)
        hits = store.search("contract", reader=OWNER, top_k=10)
        ids = [h["id"] for h in hits]
        assert shared in ids and priv["id"] in ids
        assert all(h["ns"] in ("_shared", PRIVATE_NS) for h in hits)

    def test_without_identity_default_is_shared_only(self, store: MemoryStore):
        """身份未知：缺省检索退化为单 _shared（与旧版一致）。"""
        store.write("contract dual channel private", type="fact", source=OWNER, ns=PRIVATE_NS)
        _seed_shared(store)
        hits = store.search("contract", top_k=10)
        assert hits and all(h["ns"] == "_shared" for h in hits)

    def test_explicit_shared_ns_excludes_private(self, store: MemoryStore):
        """显式 ns 是精确语义：显式 _shared 即便有身份也不并私有通道。"""
        store.write("contract dual channel private", type="fact", source=OWNER, ns=PRIVATE_NS)
        assert store.search("dual channel private", ns="_shared", reader=OWNER) == []


class TestResultShape:
    def test_hit_keys_pinned(self, store: MemoryStore):
        """hit 形状（scoring.rank 单点）：恰好这 9 个键 + 默认内嵌 neighbors。"""
        mem = store.write("contract hit shape marker", type="fact", source="agent-a")
        (hit,) = store.search("hit shape marker")
        assert set(hit) == {
            "id", "score", "similarity", "confidence", "uses", "type", "ns", "source", "content", "neighbors",
        }
        assert hit["id"] == mem["id"]

    def test_include_neighbors_false_omits_key(self, store: MemoryStore):
        store.write("contract no neighbor marker", type="fact", source="agent-a")
        (hit,) = store.search("no neighbor marker", include_neighbors=False)
        assert "neighbors" not in hit


class TestSideEffects:
    def test_search_creates_no_commit(self, store: MemoryStore):
        """#31/ADR 0006：读路径不进审计史——search 不产生 git 提交。"""
        _seed_shared(store)
        before = commit_count(store)
        store.search("shared search marker")
        store.search("anything else", ns="_shared", reader="agent-x")
        assert commit_count(store) == before

    def test_expired_and_archived_invisible(self, store: MemoryStore):
        """活性语义（词面单路）：valid_until 已过与已归档的记忆检索不可见。"""
        store.write("contract expired fact", type="fact", source="agent-a", valid_until=days_ago(1))
        store.write("contract stale episode", type="episode", source="agent-a", created=days_ago(120))
        store.decay_sweep()
        assert store.search("expired fact") == []
        assert store.search("stale episode") == []


class TestVectorCapability:
    """显式能力声明（而非 skip）：向量召回经注入 embedder 的 seam 启用
    （bag_embedder，确定性词袋编码）；未注入 embedder 时检索降级纯词面——
    降级是缺省行为、不是被跳过的能力。"""

    def test_vector_recall_fuses_into_hits(self, vec_store: MemoryStore):
        mem = vec_store.write("docker network bridge mode notes", type="fact", source="agent-a")
        hits = vec_store.search("docker network bridge", top_k=10)
        assert [h["id"] for h in hits] == [mem["id"]]

    def test_lexical_only_store_works_without_embedder(self, store: MemoryStore):
        """vec 未装的形态：无 embedder 的 store 检索照常（纯词面），不报错不缺席。"""
        mem = store.write("lexical fallback works fine", type="fact", source="agent-a")
        assert [h["id"] for h in store.search("lexical fallback")] == [mem["id"]]
