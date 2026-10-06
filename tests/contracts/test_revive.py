"""revive 动词契约（store 层五要素 characterization）。

契约出处：#26（revive 与 get 同属按 id 读路径：私有 ns 仅属主可复活）；
#31（revive 提交消息模板）；归档可复活是「归档不丢数据」哲学的写侧出口。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, OWNER_BARE, PRIVATE_NS, commit_count, days_ago, last_message, matches


def _archived_shared(store: MemoryStore) -> dict:
    """归档一条共享 episode（created 距今 120 天 > ttl 90 且 uses=0），返回写入结果。"""
    mem = store.write("contract revive target", type="episode", source="agent-a", created=days_ago(120))
    assert mem["id"] in store.decay_sweep()
    return mem


def _archived_private(store: MemoryStore) -> dict:
    mem = store.write(
        "contract revive private", type="episode", source=OWNER, ns=PRIVATE_NS, created=days_ago(120)
    )
    assert mem["id"] in store.decay_sweep()
    return mem


class TestInputEquivalence:
    def test_unknown_id_returns_found_false(self, store: MemoryStore):
        assert store.revive("nope") == {"found": False}


class TestPermissionMatrix:
    @pytest.mark.parametrize("reader", [None, FOREIGN], ids=["no-identity", "foreign"])
    def test_private_revive_denied_stays_archived(self, store: MemoryStore, reader: str | None):
        """私有归档记忆：缺身份/外来身份 PermissionError——且拒绝原子（保持归档态）。"""
        mem = _archived_private(store)
        with pytest.raises(PermissionError):
            store.revive(mem["id"], reader=reader)
        assert store.get(mem["id"], reader=OWNER)["archived"] is True

    @pytest.mark.parametrize("reader", [OWNER, OWNER_BARE], ids=["full-form", "bare-form"])
    def test_private_revive_owner_ok(self, store: MemoryStore, reader: str):
        mem = _archived_private(store)
        out = store.revive(mem["id"], reader=reader)
        assert out["found"] is True and out["archived"] is False


class TestSideEffects:
    def test_commits_with_template_message(self, store: MemoryStore):
        mem = _archived_shared(store)
        before = commit_count(store)
        store.revive(mem["id"])
        assert commit_count(store) == before + 1
        m = matches("revive", last_message(store))
        assert m["id"] == mem["id"]

    def test_moves_file_to_active_and_back_into_index(self, store: MemoryStore):
        """复活 = 文件搬回活动区 + 索引同步（重新可检索）——归档必不在索引的镜像面。"""
        mem = _archived_shared(store)
        assert store.search("revive target") == []
        store.revive(mem["id"])
        assert (store.ns_root / "_shared" / "episode" / f"{mem['id']}.md").exists()
        assert not (store.archive_root / "_shared" / "episode" / f"{mem['id']}.md").exists()
        assert [h["id"] for h in store.search("revive target")] == [mem["id"]]

    def test_revive_of_active_memory_is_zero_op(self, store: MemoryStore):
        """活动记忆的 revive 是幂等零操作：found:True 照回，但不产生提交。"""
        mem = store.write("contract revive active noop", type="episode", source="agent-a")
        before = commit_count(store)
        out = store.revive(mem["id"])
        assert out["found"] is True and out["archived"] is False
        assert commit_count(store) == before

    def test_missing_id_leaves_no_commit(self, store: MemoryStore):
        _archived_shared(store)
        before = commit_count(store)
        store.revive("nope")
        assert commit_count(store) == before
