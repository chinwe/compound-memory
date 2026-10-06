"""feedback 动词契约（store 层五要素 characterization）。

契约出处：#26 P1（置信度精确公式）/ P2（side effects 含归档自动复活）；
#31（feedback 每次一 commit，消息模板）。复利闭环是一等公民——公式漂移在此必红。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE
from anchors import FOREIGN, OWNER, PRIVATE_NS, commit_count, days_ago, last_message, matches


def _shared_mem(store: MemoryStore, source: str = "agent-a") -> dict:
    return store.write("contract feedback target", type="fact", source=source, key="fb")


class TestConfidenceFormula:
    """P1：conf bump = 0.1/次 + 0.15 仅当「新验证者 ∧ ≠source」；
    validated_by 去重（同 agent 重复反馈不再加验证分）；round 3 位。"""

    def test_source_self_feedback_bumps_point_one(self, store: MemoryStore):
        """source 自己首次反馈：只加使用分 +0.1（agent == source 不算新验证者）。"""
        mem = _shared_mem(store, source="agent-a")
        out = store.feedback(mem["id"], "agent-a")
        assert out["uses"] == 1
        assert out["confidence"] == 0.6
        assert out["validated_by"] == ["agent-a"]

    def test_new_validator_cross_agent_bumps_point_two_five(self, store: MemoryStore):
        """不同 agent 首次验证：+0.1 + 0.15 = +0.25。"""
        mem = _shared_mem(store, source="agent-a")
        out = store.feedback(mem["id"], "agent-b")
        assert out["confidence"] == 0.75
        assert out["validated_by"] == ["agent-b"]

    def test_repeat_validator_gets_use_bump_only(self, store: MemoryStore):
        """同 agent 重复反馈：validated_by 去重后只 +0.1——验证分不重复发放。"""
        mem = _shared_mem(store, source="agent-a")
        store.feedback(mem["id"], "agent-b")  # 0.75
        out = store.feedback(mem["id"], "agent-b")  # 去重：只 +0.1
        assert out["confidence"] == 0.85
        assert out["validated_by"] == ["agent-b"]

    def test_source_later_joins_as_validator_without_cross_bump(self, store: MemoryStore):
        """source 首次出现在 validated_by：agent == source，只加使用分。"""
        mem = _shared_mem(store, source="agent-a")
        store.feedback(mem["id"], "agent-b")  # 0.75, validated_by=[b]
        out = store.feedback(mem["id"], "agent-a")  # a != b 是新验证者但 == source：+0.1
        assert out["confidence"] == 0.85
        assert out["validated_by"] == ["agent-b", "agent-a"]

    def test_confidence_capped_at_one(self, store: MemoryStore):
        mem = _shared_mem(store, source="agent-a")
        for _ in range(10):
            store.feedback(mem["id"], "agent-a")
        assert store.get(mem["id"])["confidence"] == 1.0

    def test_confidence_rounded_to_three_decimals(self, store: MemoryStore):
        """round 3 位可观测：0.5+0.1+0.1 的浮点尾差（0.7999…）被修到 0.8。"""
        mem = _shared_mem(store, source="agent-a")
        store.feedback(mem["id"], "agent-a")
        store.feedback(mem["id"], "agent-a")
        out = store.feedback(mem["id"], "agent-a")
        assert out["confidence"] == 0.8


class TestSideEffects:
    """P2：uses+1、last_used=today、归档记忆自动复活（活动区 + 索引同步）、单次 commit。"""

    def test_uses_and_last_used_updated(self, store: MemoryStore):
        mem = _shared_mem(store)
        out = store.feedback(mem["id"], "agent-b")
        assert out["uses"] == 1
        assert out["last_used"] == CLOCK_DATE.isoformat()

    def test_commits_with_template_message(self, store: MemoryStore):
        mem = _shared_mem(store, source="agent-a")
        before = commit_count(store)
        store.feedback(mem["id"], "agent-b")
        assert commit_count(store) == before + 1  # #31：feedback 维持每次一 commit
        m = matches("feedback", last_message(store))
        assert m["id"] == mem["id"]
        assert m["agent"] == "agent-b"
        assert m["uses"] == "1"
        assert m["conf"] == "0.75"

    def test_archived_memory_auto_revives(self, store: MemoryStore):
        """P2：归档记忆被 feedback 自动复活——文件回活动区、索引同步（重新可检索）、
        且只产生一条 feedback 消息的提交（不是 revive 消息）。"""
        mem = store.write("contract feedback revive", type="episode", source="agent-a", created=days_ago(120))
        store.decay_sweep()
        assert store.search("feedback revive") == []  # 归档必不在索引

        before = commit_count(store)
        out = store.feedback(mem["id"], "agent-b")
        assert out["archived"] is False
        assert out["uses"] == 1
        assert commit_count(store) == before + 1
        matches("feedback", last_message(store))
        assert (store.ns_root / "_shared" / "episode" / f"{mem['id']}.md").exists()
        assert [h["id"] for h in store.search("feedback revive")] == [mem["id"]]  # 复活回到索引

    def test_missing_id_returns_found_false_no_commit(self, store: MemoryStore):
        before = commit_count(store)
        assert store.feedback("nope", "agent-b") == {"found": False}
        assert commit_count(store) == before


class TestPermissionMatrix:
    def test_shared_feedback_any_agent(self, store: MemoryStore):
        mem = _shared_mem(store, source=FOREIGN)
        assert store.feedback(mem["id"], "agent-b")["found"] is True

    def test_private_feedback_denied_atomically(self, store: MemoryStore):
        """外来 agent 对私有记忆的反馈：PermissionError 且原子（uses/confidence 不动）。"""
        priv = store.write("contract private feedback", type="fact", source=OWNER, ns=PRIVATE_NS)
        with pytest.raises(PermissionError):
            store.feedback(priv["id"], FOREIGN)
        got = store.get(priv["id"], reader=OWNER)
        assert got["uses"] == 0 and got["confidence"] == 0.5

    def test_private_feedback_owner_ok(self, store: MemoryStore):
        priv = store.write("contract private feedback", type="fact", source=OWNER, ns=PRIVATE_NS)
        assert store.feedback(priv["id"], OWNER)["uses"] == 1
