"""forget 动词契约（store 层五要素 characterization）。

契约出处：ADR-0009（#47 裁决，#48 实现）——终态受控删除：文件经既有
remover seam 物理移出（活动区或归档区）+ 恰好一条 forget 提交留痕，内容
仅存 git 历史。墓碑 / 归档终态化 / 历史改写均被 ADR 否决，本契约不钉它们；
读路径零改动（文件不存在 ⇒ get/search/邻居/蒸馏天然不可见），stats 不设
forgotten 计数，被遗忘记忆无复活通道。入口仅 store + CLI（MCP 恰好
5 tool 红线不动）。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, OWNER_BARE, PRIVATE_NS, commit_count, days_ago, last_message, matches


def _archived_episode(store: MemoryStore) -> dict:
    """归档一条共享 episode（created 距今 120 天 > ttl 90 且 uses=0），返回写入结果。"""
    mem = store.write("contract forget target", type="episode", source="agent-a", created=days_ago(120))
    assert mem["id"] in store.decay_sweep()
    return mem


class TestFoundSemantics:
    def test_unknown_id_returns_found_false(self, store: MemoryStore):
        assert store.forget("nope", "agent-a") == {"found": False}

    def test_forget_is_idempotent(self, store: MemoryStore):
        """不存在/已遗忘都返回 {"found": False}——脚本可安全重跑（spec #52 story 13）。"""
        mem = store.write("contract forget once", type="episode", source="agent-a")
        assert store.forget(mem["id"], "agent-a")["found"] is True
        assert store.forget(mem["id"], "agent-a") == {"found": False}

    def test_returns_pre_deletion_snapshot(self, store: MemoryStore):
        mem = store.write("contract forget snapshot", type="fact", source="agent-a", key="forget-snap")
        out = store.forget(mem["id"], "agent-a")
        assert out["found"] is True
        assert out["id"] == mem["id"]
        assert out["content"] == "contract forget snapshot"
        assert out["type"] == "fact" and out["key"] == "forget-snap"
        assert out["archived"] is False  # 快照是删除前的真实状态

    def test_file_removed_and_all_read_paths_blind(self, store: MemoryStore):
        """终态 = 文件物理移出：读路径零过滤、零改动，不可见是文件不存在的自然结果。"""
        mem = store.write("contract forget visibility", type="episode", source="agent-a")
        path = store.ns_root / "_shared" / "episode" / f"{mem['id']}.md"
        assert path.exists()
        # 可见性断言显式放大 top_k（截断会伪装成「不可见」）
        assert [h["id"] for h in store.search("forget visibility", top_k=50)] == [mem["id"]]
        assert store.forget(mem["id"], "agent-a")["found"] is True
        assert not path.exists()
        assert store.get(mem["id"]) == {"found": False}
        assert store.search("forget visibility", top_k=50) == []

    def test_no_revival_channel(self, store: MemoryStore):
        """被遗忘记忆的 feedback/revive 返回 found: False——终态，系统内不可逆。"""
        mem = store.write("contract forget terminal", type="episode", source="agent-a")
        assert store.forget(mem["id"], "agent-a")["found"] is True
        assert store.feedback(mem["id"], "agent-a") == {"found": False}
        assert store.revive(mem["id"]) == {"found": False}

    def test_missing_id_leaves_no_commit(self, store: MemoryStore):
        before = commit_count(store)
        assert store.forget("nope", "agent-a") == {"found": False}
        assert commit_count(store) == before


class TestPermissionMatrix:
    def test_private_forget_foreign_denied_is_atomic(self, store: MemoryStore):
        """私有 ns 仅属主可遗忘（role=agent，与 feedback 同规——防外来 agent 删他人私有记忆）；
        拒绝原子：文件保留、零提交、记忆仍可读。"""
        mem = store.write("contract forget private", type="episode", source=OWNER, ns=PRIVATE_NS)
        before = commit_count(store)
        with pytest.raises(PermissionError):
            store.forget(mem["id"], FOREIGN)
        assert (store.ns_root / PRIVATE_NS / "episode" / f"{mem['id']}.md").exists()
        assert commit_count(store) == before
        assert store.get(mem["id"], reader=OWNER)["found"] is True

    @pytest.mark.parametrize("agent", [OWNER, OWNER_BARE], ids=["full-form", "bare-form"])
    def test_private_forget_owner_ok(self, store: MemoryStore, agent: str):
        mem = store.write("contract forget private owner", type="episode", source=OWNER, ns=PRIVATE_NS)
        assert store.forget(mem["id"], agent)["found"] is True
        assert store.get(mem["id"], reader=OWNER) == {"found": False}


class TestSideEffects:
    def test_commits_exactly_once_with_template_message(self, store: MemoryStore):
        mem = store.write("contract forget commit", type="episode", source="agent-a")
        before = commit_count(store)
        store.forget(mem["id"], "agent-a")
        assert commit_count(store) == before + 1
        m = matches("forget", last_message(store))
        assert m["id"] == mem["id"] and m["agent"] == "agent-a"

    def test_commit_message_carries_reason(self, store: MemoryStore):
        mem = store.write("contract forget reason", type="episode", source="agent-a")
        store.forget(mem["id"], "agent-a", reason="superseded by proj-x rollout")
        m = matches("forget", last_message(store))
        assert m["reason"] == "superseded by proj-x rollout"

    def test_reason_is_single_lined(self, store: MemoryStore):
        """reason 单行化（ADR-0009）：换行/制表不得拆散单行审计提交。"""
        mem = store.write("contract forget oneline", type="episode", source="agent-a")
        store.forget(mem["id"], "agent-a", reason="wrong fact\nsecond line\ttail")
        message = last_message(store)
        assert "\n" not in message
        m = matches("forget", message)
        assert m["reason"] == "wrong fact second line tail"

    def test_reason_capped_at_eighty_chars(self, store: MemoryStore):
        mem = store.write("contract forget cap", type="episode", source="agent-a")
        store.forget(mem["id"], "agent-a", reason="r" * 200)
        m = matches("forget", last_message(store))
        assert len(m["reason"]) == 80

    def test_whitespace_reason_omits_reason_segment(self, store: MemoryStore):
        mem = store.write("contract forget blank", type="episode", source="agent-a")
        store.forget(mem["id"], "agent-a", reason="  \n\t ")
        m = matches("forget", last_message(store))
        assert m["reason"] is None


class TestReviewQueueInteraction:
    def _conflicting_pair(self, store: MemoryStore) -> tuple[dict, dict]:
        old = store.write("contract forget old version", type="fact", source="agent-a", key="forget-conflict")
        new = store.write("contract forget new version", type="fact", source="agent-b", key="forget-conflict")
        assert len(store.review_queue()) == 1
        return old, new

    def test_forget_clears_rows_of_forgotten_memory(self, store: MemoryStore):
        """行含正文片段而本体已遗忘——幽灵行会误导后续裁决，forget 顺带清行（ADR-0009）。"""
        _old, new = self._conflicting_pair(store)
        assert store.forget(new["id"], "agent-b")["found"] is True
        assert store.review_queue() == []

    def test_forget_clears_row_when_forgotten_side_is_old(self, store: MemoryStore):
        old, _new = self._conflicting_pair(store)
        assert store.forget(old["id"], "agent-a")["found"] is True
        assert store.review_queue() == []

    def test_row_clearing_adds_no_extra_commit(self, store: MemoryStore):
        """删文件 + 清队列行 + 登记提交收进恰好一条 commit（遗忘审计 = git log 的粒度契约）。"""
        _old, new = self._conflicting_pair(store)
        before = commit_count(store)
        store.forget(new["id"], "agent-b")
        assert commit_count(store) == before + 1

    def test_forget_without_queue_rows_is_silent(self, store: MemoryStore):
        """幂等清行：无行是常态，不报错（区别于 review-resolve 按 ids 的未命中 ValueError）。"""
        mem = store.write("contract forget no rows", type="episode", source="agent-a")
        assert store.forget(mem["id"], "agent-a")["found"] is True


class TestForgetFromArchive:
    def test_archived_memory_is_forgettable(self, store: MemoryStore):
        """归档记忆同样可 forget（作用域 = 活动区 ∪ 归档区）；快照如实反映删除前的归档态。"""
        mem = _archived_episode(store)
        assert store.get(mem["id"])["archived"] is True
        archive_file = store.archive_root / "_shared" / "episode" / f"{mem['id']}.md"
        assert archive_file.exists()
        out = store.forget(mem["id"], "agent-a")
        assert out["found"] is True
        assert out["archived"] is True
        assert not archive_file.exists()
        assert store.get(mem["id"]) == {"found": False}
        m = matches("forget", last_message(store))
        assert m["id"] == mem["id"]


class TestDanglingLinks:
    def test_links_to_forgotten_memory_stay(self, store: MemoryStore):
        """links 悬空容忍（ADR-0009）：指向被遗忘记忆的 links 不摘除——
        find→None 容错已覆盖邻居召回与蒸馏候选，内部留痕有审计价值。"""
        a = store.write("contract forget link holder", type="episode", source="agent-a")
        b = store.write("contract forget link target", type="episode", source="agent-a")
        store.link(a["id"], b["id"], agent="agent-a")
        assert store.forget(b["id"], "agent-a")["found"] is True
        out = store.get(a["id"])
        assert out["found"] is True
        assert out["links"] == [b["id"]]  # 悬空链保留，不摘除
        assert out["neighbors"] == []  # 死链不产出邻居


class TestVectorSeam:
    def test_forgotten_memory_leaves_vector_recall(self, vec_store: MemoryStore):
        """向量缓存移除走既有 sync 收口（archived ⇒ 引擎移除）——遗忘后向量路同样不可见。"""
        mem = vec_store.write("contract forget vector visibility", type="episode", source="agent-a")
        assert [h["id"] for h in vec_store.search("vector visibility", top_k=10)] == [mem["id"]]
        assert vec_store.forget(mem["id"], "agent-a")["found"] is True
        assert vec_store.search("vector visibility", top_k=10) == []
