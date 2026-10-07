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


class TestNeighborLiveness:
    """邻居带出活性走单点 search_mod.active_neighbors（ADR-0010 过滤面边界），
    两个面策略显式分维（参数即 seam）：过期邻居一律不展开正文（CONTEXT.md：
    valid_until 次日起退出邻居召回，两侧同规）；归档邻居在 get 显式寻址/溯源面
    保留（蒸馏产物带出归档源，test_distill.py::test_apply_is_atomic_write_links_
    archive_single_commit 钉），在 search 检索面排除（检索只呈现活动知识）。
    get 本体恒可读不受影响（显式寻址语义保留）。"""

    def test_archived_neighbor_expanded_for_provenance(self, store: MemoryStore):
        anchor = store.write("contract get neighbor liveness anchor", type="fact", source="agent-a")
        side = store.write("contract get neighbor liveness side", type="episode", source="agent-a", created=days_ago(120))
        store.link(anchor["id"], side["id"])
        store.decay_sweep()
        got = store.get(anchor["id"])
        assert side["id"] in got["links"]
        assert side["id"] in {n["id"] for n in got["neighbors"]}  # 溯源面：归档邻居带出

    def test_expired_neighbor_not_expanded(self, store: MemoryStore):
        anchor = store.write("contract get neighbor expiry anchor", type="fact", source="agent-a")
        side = store.write("contract get neighbor expiry side", type="fact", source="agent-a", valid_until=days_ago(1))
        store.link(anchor["id"], side["id"])
        got = store.get(anchor["id"])
        assert side["id"] in got["links"]
        assert all(n["id"] != side["id"] for n in got["neighbors"])
