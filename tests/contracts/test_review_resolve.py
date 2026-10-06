"""review_resolve 动词契约（store 层五要素 characterization）。

契约出处：#26 P4（resolve 精确语义）与 D2（私有行属主收口、展示维持全量）；
#31（review resolve 提交消息模板）。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, OWNER_BARE, PRIVATE_NS, commit_count, last_message, matches


def _shared_conflict(store: MemoryStore, key: str = "rk") -> tuple[str, str]:
    """制造一对同 key fact 冲突（内容不同才入队），返回 (old_id, new_id)。"""
    old = store.write(f"contract resolve old {key}", type="fact", source=FOREIGN, key=key)
    new = store.write(f"contract resolve new {key}", type="fact", source=FOREIGN, key=key)
    return old["id"], new["id"]


def _private_conflict(store: MemoryStore, key: str = "priv-rk") -> tuple[str, str]:
    old = store.write(f"contract resolve priv old {key}", type="fact", source=OWNER, ns=PRIVATE_NS, key=key)
    new = store.write(f"contract resolve priv new {key}", type="fact", source=OWNER, ns=PRIVATE_NS, key=key)
    return old["id"], new["id"]


class TestInputEquivalence:
    def test_no_ids_no_all_is_caller_error(self, store: MemoryStore):
        with pytest.raises(ValueError, match="ids or --all"):
            store.review_resolve([])

    def test_ids_and_all_are_exclusive(self, store: MemoryStore):
        with pytest.raises(ValueError, match="either"):
            store.review_resolve(["x"], all=True)

    def test_unknown_id_rejected_atomically(self, store: MemoryStore):
        """P4：id 未命中任一行 ⇒ ValueError 原子拒绝（队列原样保留），零提交。"""
        old, _ = _shared_conflict(store)
        before = commit_count(store)
        with pytest.raises(ValueError, match="nope"):
            store.review_resolve([old, "nope"])
        assert len(store.review_queue()) == 1
        assert commit_count(store) == before


class TestPermissionMatrix:
    """D2：清行按属主可见性收口；展示维持全量。"""

    def test_shared_rows_resolve_without_reader(self, store: MemoryStore):
        old, _ = _shared_conflict(store)
        assert store.review_resolve([old])["resolved"] == 1

    @pytest.mark.parametrize("reader", [None, FOREIGN], ids=["no-identity", "foreign"])
    def test_private_row_denied_atomically(self, store: MemoryStore, reader: str | None):
        """显式点名私有行：缺身份/外来身份均 PermissionError，队列原样、零提交。"""
        old, _ = _private_conflict(store)
        before = commit_count(store)
        with pytest.raises(PermissionError):
            store.review_resolve([old], reader=reader)
        assert len(store.review_queue()) == 1
        assert commit_count(store) == before

    @pytest.mark.parametrize("reader", [OWNER, OWNER_BARE], ids=["full-form", "bare-form"])
    def test_private_row_owner_resolves(self, store: MemoryStore, reader: str):
        old, _ = _private_conflict(store)
        out = store.review_resolve([old], reader=reader)
        assert out["resolved"] == 1 and out["archived"] == [old]
        assert store.review_queue() == []

    def test_all_filters_private_rows_for_identity_less_caller(self, store: MemoryStore):
        """--all 同样按属主可见性过滤：无身份只清 _shared 行，私有行保留并如实计数。"""
        _private_conflict(store)
        pub_old, _ = _shared_conflict(store, key="pub-rk")
        out = store.review_resolve(all=True)
        assert out == {"resolved": 1, "remaining": 1, "archived": []}
        remaining = store.review_queue()
        assert len(remaining) == 1 and PRIVATE_NS in remaining[0] and pub_old not in remaining[0]

    def test_all_with_owner_reader_clears_own_private_rows(self, store: MemoryStore):
        _private_conflict(store)
        out = store.review_resolve(all=True, reader=OWNER)
        assert out == {"resolved": 1, "remaining": 0, "archived": []}

    def test_display_stays_full_without_identity(self, store: MemoryStore):
        """D2 张力面：展示不收口——无身份调用方 review_queue() 仍列出私有行
        （CLI 是本机信任边界；MCP 5 tool 不暴露队列）。"""
        old, new = _private_conflict(store)
        lines = store.review_queue()
        assert len(lines) == 1 and old in lines[0] and new in lines[0]


class TestContradictionRows:
    """ADR-0007 独立行型：`- <date> contradiction <mem_id>: by <agent> (<note 前 40 字>)`。
    行格式单一定义点（ReviewQueue）内扩展；行 roundtrip 与裁决二选一在此钉住。"""

    def _contradiction_row(self, store: MemoryStore, key: str = "ctr") -> str:
        mem = store.write(f"contract review contradiction {key}", type="fact", source=FOREIGN, key=key)
        store.feedback(mem["id"], "agent-b", outcome="contradiction")
        return mem["id"]

    def test_row_roundtrip_display_and_resolve(self, store: MemoryStore):
        """登记行可展示（行内含 mem_id/agent/正文片段）、可按 id 清行。"""
        mem_id = self._contradiction_row(store)
        lines = store.review_queue()
        assert len(lines) == 1
        assert mem_id in lines[0] and "agent-b" in lines[0] and "contradiction" in lines[0]
        out = store.review_resolve([mem_id])
        assert out["resolved"] == 1
        assert store.review_queue() == []

    def test_uphold_commit_message_template(self, store: MemoryStore):
        """裁决「维持」的提交消息带 (upheld: ...) 段（契约变更：ADR-0007 折算留痕）。"""
        mem_id = self._contradiction_row(store)
        before = commit_count(store)
        store.review_resolve([mem_id], uphold=True)
        assert commit_count(store) == before + 1
        m = matches("review_resolve", last_message(store))
        assert m["n"] == "1"
        assert m["upheld"] == mem_id
        assert m["ids"] is None  # 未归档任何记忆


class TestSideEffects:
    def test_commits_with_archive_suffix_template(self, store: MemoryStore):
        old, _ = _shared_conflict(store)
        before = commit_count(store)
        store.review_resolve([old])
        assert commit_count(store) == before + 1
        m = matches("review_resolve", last_message(store))
        assert m["n"] == "1"
        assert m["ids"] == old

    def test_commit_without_archive_suffix_when_nothing_archived(self, store: MemoryStore):
        """废置方已归档时：清行仍提交，但消息不带 archived 后缀（不虚报归档动作）。"""
        old, _ = _shared_conflict(store)
        src = store.find(old)
        assert src is not None
        store._archive(src)
        store.review_resolve([old])
        m = matches("review_resolve", last_message(store))
        assert m["ids"] is None

    def test_zero_resolved_leaves_no_commit(self, store: MemoryStore):
        """--all 空转（resolved=0）：幂等且零提交。"""
        _shared_conflict(store)
        store.review_resolve(all=True)
        before = commit_count(store)
        assert store.review_resolve(all=True) == {"resolved": 0, "remaining": 0, "archived": []}
        assert commit_count(store) == before

    def test_archives_only_dropped_side_survivor_stays(self, store: MemoryStore):
        """P4：传入 id = 裁决废置方——清行同时归档它，对侧保留活动区。"""
        old, new = _shared_conflict(store)
        out = store.review_resolve([old])
        assert out["rows"] == [{"old": old, "new": new}]
        assert store.get(old)["archived"] is True
        assert store.get(new)["archived"] is False

    def test_all_clears_without_archiving_or_rows(self, store: MemoryStore):
        """P4：--all 只清行不归档、返回不携带 rows（无裁决信息不产出明细）。"""
        old, new = _shared_conflict(store)
        out = store.review_resolve(all=True)
        assert out == {"resolved": 1, "remaining": 0, "archived": []}
        assert store.get(old)["archived"] is False
        assert store.get(new)["archived"] is False
