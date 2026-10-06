"""get（按 id 读）动词契约（store 层五要素 characterization）。

契约出处：#25（按 id 动词恒含 found 键）/ #26 D3（get 恒读不受有效期影响）。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, OWNER_BARE, PRIVATE_NS, commit_count, days_ago


class TestInputEquivalence:
    def test_unknown_id_returns_found_false_envelope(self, store: MemoryStore):
        """目标不存在：正常返回 {"found": False}——不抛错（按 id 动词信封约定）。"""
        assert store.get("nope") == {"found": False}

    @pytest.mark.parametrize("bad_id", ["*", "../x", "id with space", "a/b"])
    def test_malformed_id_is_nonexistent_not_error(self, store: MemoryStore, bad_id: str):
        """非法 mem_id（glob 元字符/路径分隔）语义等价于「不可能存在」⇒ found:False，
        不抛错——抛错会炸掉邻居召回的「宁缺勿炸」降级（2026-10-05 审计 P2-3）。"""
        assert store.get(bad_id) == {"found": False}


class TestPermissionMatrix:
    def test_shared_get_needs_no_reader(self, store: MemoryStore):
        mem = store.write("contract shared get", type="fact", source=FOREIGN)
        assert store.get(mem["id"])["found"] is True

    @pytest.mark.parametrize("reader", [None, FOREIGN], ids=["no-identity", "foreign"])
    def test_private_get_denied_without_owner(self, store: MemoryStore, reader: str | None):
        priv = store.write("contract private get", type="fact", source=OWNER, ns=PRIVATE_NS)
        with pytest.raises(PermissionError):
            store.get(priv["id"], reader=reader)

    @pytest.mark.parametrize("reader", [OWNER, OWNER_BARE], ids=["full-form", "bare-form"])
    def test_private_get_owner_ok(self, store: MemoryStore, reader: str):
        priv = store.write("contract private get", type="fact", source=OWNER, ns=PRIVATE_NS)
        assert store.get(priv["id"], reader=reader)["found"] is True


class TestResultShape:
    def test_found_true_returns_full_memory(self, store: MemoryStore):
        mem = store.write("contract get shape", type="fact", source="agent-a", key="shape")
        got = store.get(mem["id"])
        assert got["found"] is True
        assert got["id"] == mem["id"]
        assert got["content"] == "contract get shape"
        assert got["archived"] is False
        assert got["key"] == "shape"

    def test_neighbors_embedded_by_default(self, store: MemoryStore):
        a = store.write("contract get neighbor anchor", type="fact", source="agent-a")
        b = store.write("contract get neighbor side", type="fact", source="agent-a")
        store.link(a["id"], b["id"])
        got = store.get(a["id"])
        assert [n["id"] for n in got["neighbors"]] == [b["id"]]

    def test_include_neighbors_false_omits_key(self, store: MemoryStore):
        a = store.write("contract get nn anchor", type="fact", source="agent-a")
        b = store.write("contract get nn side", type="fact", source="agent-a")
        store.link(a["id"], b["id"])
        assert "neighbors" not in store.get(a["id"], include_neighbors=False)

    def test_links_output_redacts_cross_ns(self, store: MemoryStore):
        """遗留跨 ns 链脱敏：links 输出与邻居都不暴露对侧 id（读侧边界的一部分）。"""
        priv = store.write("contract redaction priv", type="fact", source=OWNER, ns=PRIVATE_NS)
        bridge = store.write("contract redaction bridge", type="episode", source=FOREIGN)
        for mem, target in ((bridge, priv), (priv, bridge)):
            mem_obj = store.find(mem["id"])
            assert mem_obj is not None
            mem_obj.links.append(target["id"])
            store._save(mem_obj)
        store.rebuild_index()

        got_pub = store.get(bridge["id"])
        assert got_pub["links"] == []
        assert got_pub.get("neighbors", []) == []


class TestReadPathGuarantees:
    def test_archived_memory_still_gettable(self, store: MemoryStore):
        """归档不丢数据：get 恒可读（检索不可见、按 id 可读）。"""
        mem = store.write("contract archived get", type="episode", source="agent-a", created=days_ago(120))
        store.decay_sweep()
        got = store.get(mem["id"])
        assert got["found"] is True and got["archived"] is True

    def test_expired_memory_still_gettable(self, store: MemoryStore):
        """D3 邻接：valid_until 只管检索可见性，get 恒读。"""
        mem = store.write("contract expired get", type="fact", source="agent-a", valid_until=days_ago(1))
        assert store.get(mem["id"])["found"] is True

    def test_get_creates_no_commit(self, store: MemoryStore):
        """读路径不进审计史（ADR 0006）：get 不产生 git 提交。"""
        mem = store.write("contract get commit probe", type="fact", source="agent-a")
        before = commit_count(store)
        store.get(mem["id"])
        assert commit_count(store) == before
