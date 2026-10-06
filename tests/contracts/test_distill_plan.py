"""distill_plan 动词契约（store 层五要素 characterization）。

契约出处：#26（蒸馏框架：三段式分工、归档区不参与、过期跳过）；
#25（Tier 1 全五要素，但深度行为测试在 tests/test_distill.py——这里钉形状与门禁）。
plan 是只读扫描：不产生任何 git 提交（side effects 为零是契约的一部分）。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, PRIVATE_NS, commit_count, days_ago


class TestInputEquivalence:
    def test_illegal_ns_is_caller_error(self, store: MemoryStore):
        with pytest.raises(ValueError):
            store.distill_plan(ns="not-a-ns")

    def test_quiet_store_returns_empty_both_sections(self, store: MemoryStore):
        plan = store.distill_plan()
        assert plan["candidates"] == []
        assert plan["key_duplicates"] == []

    def test_activity_gate_excludes_dead_memories(self, store: MemoryStore):
        """活性门等价类：uses=0 的死本金不进候选（蒸馏只针对被验证过的活记忆）。"""
        store.write("contract plan dead", type="episode", source="agent-a")
        used = store.write("contract plan used", type="episode", source="agent-a")
        store.feedback(used["id"], "agent-a")
        plan = store.distill_plan()
        assert [c["id"] for c in plan["candidates"]] == [used["id"]]

    def test_archived_and_expired_excluded(self, store: MemoryStore):
        """归档区不参与蒸馏（扫描只看活动区）；过期事实不该被蒸馏固化进新产物。"""
        store.write("contract plan stale", type="episode", source="agent-a", created=days_ago(120))
        store.decay_sweep()
        store.write("contract plan expired", type="fact", source="agent-a", valid_until=days_ago(1))
        # min_uses=0 + 全开窗口排除其余变量：剩下的排除原因只有归档与过期
        assert store.distill_plan(min_uses=0, window_days=365)["candidates"] == []


class TestPermissionMatrix:
    @pytest.mark.parametrize("reader", [None, FOREIGN], ids=["no-identity", "foreign"])
    def test_private_ns_denied_without_owner(self, store: MemoryStore, reader: str | None):
        """候选带正文返回：扫私有 ns 与 get/search 同规则（凡返回正文必过门）。"""
        store.write("contract plan private", type="fact", source=OWNER, ns=PRIVATE_NS)
        with pytest.raises(PermissionError):
            store.distill_plan(ns=PRIVATE_NS, reader=reader)

    def test_private_ns_owner_gets_candidates_with_content(self, store: MemoryStore):
        mem = store.write("contract plan private", type="fact", source=OWNER, ns=PRIVATE_NS)
        store.feedback(mem["id"], OWNER)  # 过活性门
        plan = store.distill_plan(ns=PRIVATE_NS, reader=OWNER)
        assert [c["id"] for c in plan["candidates"]] == [mem["id"]]
        assert plan["candidates"][0]["content"] == "contract plan private"


class TestResultShape:
    def test_top_level_keys_pinned(self, store: MemoryStore):
        plan = store.distill_plan(window_days=7, min_uses=2, min_confidence=0.6, ns="_shared")
        assert set(plan) == {
            "window_days", "min_uses", "min_confidence", "ns", "candidates", "key_duplicates",
        }
        assert (plan["window_days"], plan["min_uses"], plan["min_confidence"], plan["ns"]) == (
            7, 2, 0.6, "_shared",
        )

    def test_candidate_keys_pinned(self, store: MemoryStore):
        used = store.write("contract plan shape", type="episode", source="agent-a", key="pshape")
        store.feedback(used["id"], "agent-a")
        (cand,) = store.distill_plan()["candidates"]
        assert set(cand) == {
            "id", "type", "key", "uses", "confidence", "created", "last_used", "content",
            "merge_with", "possible_dup_of", "promotion_candidate",
        }


class TestSideEffects:
    def test_plan_creates_no_commit(self, store: MemoryStore):
        """plan 是确定性只读扫描：零 git 提交（蒸馏的写动作全部在 apply 侧）。"""
        used = store.write("contract plan commit probe", type="episode", source="agent-a")
        store.feedback(used["id"], "agent-a")
        before = commit_count(store)
        store.distill_plan(min_uses=0)
        store.distill_plan(ns=PRIVATE_NS, reader=OWNER)  # 门禁拒绝路径同样零提交
        assert commit_count(store) == before
