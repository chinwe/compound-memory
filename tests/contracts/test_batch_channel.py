"""batch 动词契约（store 层五要素 characterization）。

batch 是批量落库正门（#25 Tier 1 第 10 项）：逐条校验写穿、批尾一次
索引 flush + 一次 commit；失败语义「落地即已提交」（partial 后原样上抛）。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE, sandbox_safe_remove
from anchors import commit_count, last_message, matches


def _batch_writes(store: MemoryStore, n: int, marker: str = "bmark") -> list[dict]:
    out = []
    for i in range(n):
        out.append(store.write(f"{marker} contract batch item {i}", type="fact", source="agent-a"))
    return out


class TestInputEquivalence:
    def test_nested_batch_is_caller_error(self, store: MemoryStore):
        with store.batch():
            with pytest.raises(ValueError, match="nested"):
                with store.batch():
                    pass

    def test_per_item_validation_enforced_inside_batch(self, store: MemoryStore):
        """写穿语义：批内条目照走逐条校验——非法 type 抛 ValueError，
        之前的合法条目以 partial 提交落地后异常原样上抛。"""
        with pytest.raises(ValueError, match="type must be one of"):
            with store.batch():
                _batch_writes(store, 2, marker="vmark")
                store.write("bad one", type="bogus", source="agent-a")
        assert len(store.search("vmark", top_k=10)) == 2  # 已写入条目不静默蒸发
        matches("batch", last_message(store))


class TestSideEffects:
    def test_n_writes_single_commit_with_template(self, store: MemoryStore):
        before = commit_count(store)
        with store.batch():
            _batch_writes(store, 3, marker="cm1")
        assert commit_count(store) == before + 1
        m = matches("batch", last_message(store))
        assert m["n"] == "3"
        assert m.group(0).endswith("entries") and "partial" not in m.group(0)

    def test_custom_message_honored_verbatim(self, store: MemoryStore):
        """自定义消息逐字透传（distill_apply 的溯源消息走这条路）。"""
        with store.batch(message="distill apply demo <- srcs"):
            _batch_writes(store, 1, marker="cm2")
        assert last_message(store) == "distill apply demo <- srcs"

    def test_empty_batch_commits_nothing(self, store: MemoryStore):
        before = commit_count(store)
        with store.batch():
            pass
        assert commit_count(store) == before

    def test_failure_commits_partial_and_reraises(self, store: MemoryStore):
        """失败语义「落地即已提交」：批内异常 ⇒ 已写入条目注明 partial 提交、
        异常原样上抛——不存在静默半提交。"""
        before = commit_count(store)
        with pytest.raises(RuntimeError, match="boom"):
            with store.batch():
                _batch_writes(store, 2, marker="pm1")
                raise RuntimeError("boom")
        assert commit_count(store) == before + 1
        m = matches("batch", last_message(store))
        assert m["n"] == "2"
        assert m.group(0).endswith("(partial)")
        assert len(store.search("pm1", top_k=10)) == 2

    def test_all_write_verbs_defer_into_batch_commit(self, store: MemoryStore):
        """单点拦截：write/feedback/link 的提交在批内全部延迟——
        三类动词合计批尾恰好一次 commit（ops 计数 = 动词数）。"""
        a = store.write("batch verb anchor a", type="fact", source="agent-a")
        b = store.write("batch verb anchor b", type="fact", source="agent-a")
        before = commit_count(store)
        with store.batch():
            _batch_writes(store, 1, marker="vb1")
            store.feedback(a["id"], "agent-b")
            store.link(a["id"], b["id"])
        assert commit_count(store) == before + 1
        m = matches("batch", last_message(store))
        assert m["n"] == "3"


class TestConsistency:
    def test_batch_entries_searchable_after_exit(self, store: MemoryStore):
        with store.batch():
            _batch_writes(store, 3, marker="vb2")
        assert len(store.search("vb2", top_k=10)) == 3

    def test_batch_flush_survives_store_reopen(self, tmp_path):
        """批尾 flush 落盘完整：重开 store（新进程形态）后批写条目可检索。"""
        from pathlib import Path

        root = tmp_path / "reopen-root"
        store = MemoryStore(root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        with store.batch():
            _batch_writes(store, 3, marker="rp1")
        reopened = MemoryStore(root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        assert len(reopened.search("rp1", top_k=10)) == 3
