"""link 动词契约（store 层五要素 characterization）。

契约出处：#26 D1（私有 ns 的 link 属主门禁——最后一个无身份写入口的收口）；
#31（link 提交消息模板）。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, OWNER_BARE, PRIVATE_NS, commit_count, last_message, matches


def _shared_pair(store: MemoryStore) -> tuple[dict, dict]:
    a = store.write("contract link shared a", type="fact", source="agent-a")
    b = store.write("contract link shared b", type="fact", source="agent-a")
    return a, b


def _private_pair(store: MemoryStore) -> tuple[dict, dict]:
    a = store.write("contract link priv a", type="fact", source=OWNER, ns=PRIVATE_NS)
    b = store.write("contract link priv b", type="fact", source=OWNER, ns=PRIVATE_NS)
    return a, b


def _links_of(store: MemoryStore, mem_id: str) -> list[str]:
    mem = store.find(mem_id)
    assert mem is not None
    return mem.links


class TestInputEquivalence:
    def test_self_link_is_caller_error(self, store: MemoryStore):
        a, _ = _shared_pair(store)
        with pytest.raises(ValueError, match="itself"):
            store.link(a["id"], a["id"])

    def test_unknown_ids_return_found_false_envelope(self, store: MemoryStore):
        """目标不存在：found:False + missing 列出全部缺失 id（按 id 动词信封约定）。"""
        assert store.link("nope", "alsono") == {"found": False, "missing": ["nope", "alsono"]}

    def test_partial_missing_lists_only_missing(self, store: MemoryStore):
        a, _ = _shared_pair(store)
        assert store.link(a["id"], "nope") == {"found": False, "missing": ["nope"]}


class TestPermissionMatrix:
    """D1：私有 ns 的 link 仅属主（可选 agent 参数，对称 feedback）；
    _shared 无需身份；缺失信封先于门禁。"""

    def test_shared_link_needs_no_identity(self, store: MemoryStore):
        a, b = _shared_pair(store)
        assert store.link(a["id"], b["id"])["found"] is True

    @pytest.mark.parametrize("agent", [None, FOREIGN], ids=["no-identity", "foreign"])
    def test_private_link_denied_atomically(self, store: MemoryStore, agent: str | None):
        """fail-closed：私有 ns 缺身份/外来身份都 PermissionError，且原子——不落半条链。"""
        a, b = _private_pair(store)
        with pytest.raises(PermissionError):
            store.link(a["id"], b["id"], agent=agent)
        assert _links_of(store, a["id"]) == []
        assert _links_of(store, b["id"]) == []

    @pytest.mark.parametrize("agent", [OWNER, OWNER_BARE], ids=["full-form", "bare-form"])
    def test_private_link_owner_ok(self, store: MemoryStore, agent: str):
        a, b = _private_pair(store)
        res = store.link(a["id"], b["id"], agent=agent)
        assert res == {"found": True, "a": a["id"], "b": b["id"], "links": [b["id"]]}

    def test_missing_reported_before_gate(self, store: MemoryStore):
        """信封约定先于门禁：私有记忆对缺失 id 的 link 返回 found:False（不是 PermissionError）。"""
        a, _ = _private_pair(store)
        assert store.link(a["id"], "nope", agent=OWNER) == {"found": False, "missing": ["nope"]}

    def test_cross_ns_link_is_caller_error_atomically(self, store: MemoryStore):
        """跨 ns 禁止（ValueError）：链会把对侧 id 写进本侧 frontmatter，是私有 id 泄漏源。"""
        priv, _ = _private_pair(store)
        shared, _ = _shared_pair(store)
        before = commit_count(store)
        with pytest.raises(ValueError, match="across namespaces"):
            store.link(shared["id"], priv["id"])
        assert commit_count(store) == before
        assert _links_of(store, shared["id"]) == []
        assert _links_of(store, priv["id"]) == []


class TestSideEffects:
    def test_commits_with_template_message(self, store: MemoryStore):
        a, b = _shared_pair(store)
        before = commit_count(store)
        store.link(a["id"], b["id"])
        assert commit_count(store) == before + 1
        m = matches("link", last_message(store))
        assert m["a"] == a["id"] and m["b"] == b["id"]

    def test_bidirectional_frontmatter_update(self, store: MemoryStore):
        a, b = _shared_pair(store)
        store.link(a["id"], b["id"])
        assert _links_of(store, a["id"]) == [b["id"]]
        assert _links_of(store, b["id"]) == [a["id"]]

    def test_relink_does_not_duplicate(self, store: MemoryStore):
        """幂等性：重复 link 不产生重复的 links 条目（frontmatter 不膨胀）。"""
        a, b = _shared_pair(store)
        store.link(a["id"], b["id"])
        store.link(a["id"], b["id"])
        assert _links_of(store, a["id"]) == [b["id"]]
        assert _links_of(store, b["id"]) == [a["id"]]

    def test_missing_ids_leave_no_commit(self, store: MemoryStore):
        _shared_pair(store)
        before = commit_count(store)
        store.link("nope", "alsono")
        assert commit_count(store) == before
