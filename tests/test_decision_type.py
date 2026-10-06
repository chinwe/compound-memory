"""decision 记忆类型（#46）：TYPE_SPEC 新行的外部行为验收。

类型知识单点在 model.TYPE_SPEC；本文件钉 decision 的行为闭环：
write/search/get/decay 全链路可用、长寿如 fact（ttl=None 不衰减）、
同 key 冲突与 fact/insight 同走 review 队列（表驱动冲突检测）、
server docstring 类型枚举从单一来源生成。
"""

from __future__ import annotations

import datetime as dt

from compound_memory import server
from compound_memory.model import MEMORY_TYPES
from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE


class TestDecisionLifecycle:
    """验收基线：write / search / get 对 decision 全部照常工作。"""

    def test_write_search_get_roundtrip(self, store: MemoryStore):
        mem = store.write(
            "we chose sqlite-vec over pgvector for the vector engine",
            type="decision",
            source="agent-a",
            key="vec-engine",
        )
        assert mem["type"] == "decision"
        assert mem["conflict"] is False
        hits = store.search("sqlite-vec vector engine", top_k=5)
        assert hits[0]["id"] == mem["id"]
        assert hits[0]["type"] == "decision"
        got = store.get(mem["id"])
        assert got["found"] is True
        assert got["content"] == mem["content"]

    def test_decision_never_decays(self, store: MemoryStore):
        """长寿如 fact：ttl=None ⇒ 400 天前的决策不进衰减归档（对照 episode 会被归档）。"""
        old = (CLOCK_DATE - dt.timedelta(days=400)).isoformat()
        decision = store.write(
            "chose fcntl flock for the write lock", type="decision", source="agent-a", created=old
        )
        episode = store.write("deploy log from long ago", type="episode", source="agent-a", created=old)
        archived = store.decay_sweep()
        assert episode["id"] in archived
        assert decision["id"] not in archived


class TestTableDrivenConflicts:
    """同 key 冲突检测表驱动化（writing.py 硬编码 (fact, insight) 的重构验收）。"""

    def test_decision_same_key_conflict_enters_review_queue(self, store: MemoryStore):
        first = store.write("engine is sqlite-vec", type="decision", source="agent-a", key="vec-engine")
        second = store.write("engine is pgvector", type="decision", source="agent-b", key="vec-engine")
        assert second["conflict"] is True
        assert second["conflicts_with"] == first["id"]
        assert len(store.review_queue()) == 1

    def test_same_decision_content_same_key_is_not_conflict(self, store: MemoryStore):
        store.write("engine is sqlite-vec", type="decision", source="agent-a", key="vec-engine")
        again = store.write("engine is sqlite-vec", type="decision", source="agent-b", key="vec-engine")
        assert again["conflict"] is False
        assert store.review_queue() == []

    def test_types_without_conflict_flag_never_enqueue(self, store: MemoryStore):
        """表驱动缺省：未标记冲突的类型（skill）同 key 不同内容不判冲突——
        新类型默认 append-only 安全，标记显式加入才走 key 更新通道。"""
        store.write("deploy via make target", type="skill", source="agent-a", key="deploy-steps")
        again = store.write("deploy via script", type="skill", source="agent-b", key="deploy-steps")
        assert again["conflict"] is False
        assert store.review_queue() == []


class TestDocstringSingleSourced:
    """server.py memory_write docstring 的类型枚举从 MEMORY_TYPES 单点生成。"""

    def test_write_docstring_lists_every_type(self):
        doc = server.memory_write.__doc__ or ""
        for mtype in MEMORY_TYPES:
            assert mtype in doc, f"type {mtype!r} missing from memory_write docstring"
