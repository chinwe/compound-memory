"""运维方法 Tier 2 薄钉（#25 决议：形状 + 错误，不重测语义）。

覆盖：find / decay_sweep / review_queue / rebuild_index / stats / git_log /
lexical_candidates。深度行为在各模块测试；这里只钉「方法存在、形状不漂移、
错误类型不漂移」——decay_sweep 额外锚定 #31 的提交消息模板。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from compound_memory.scoring import tokenize
from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE, sandbox_safe_remove
from anchors import FOREIGN, OWNER, PRIVATE_NS, commit_count, days_ago, last_message, matches


class TestFind:
    def test_unknown_id_returns_none(self, store: MemoryStore):
        assert store.find("nope") is None

    @pytest.mark.parametrize("bad_id", ["*", "../x", "a/b"])
    def test_malformed_id_returns_none_not_error(self, store: MemoryStore, bad_id: str):
        """非法 id 不进入 rglob 模式（glob 元字符曾命中任意记忆，审计 P2-3）：
        语义等价不存在 ⇒ None。"""
        assert store.find(bad_id) is None

    def test_found_returns_memory_object(self, store: MemoryStore):
        mem = store.write("tier2 find probe", type="fact", source="agent-a")
        got = store.find(mem["id"])
        assert got is not None and got.id == mem["id"]


class TestDecaySweep:
    def test_returns_archived_ids(self, store: MemoryStore):
        stale = store.write("tier2 decay stale", type="episode", source="agent-a", created=days_ago(120))
        assert store.decay_sweep() == [stale["id"]]

    def test_commits_with_template_message(self, store: MemoryStore):
        stale = store.write("tier2 decay msg", type="episode", source="agent-a", created=days_ago(120))
        store.decay_sweep()
        m = matches("decay", last_message(store))
        assert m["ids"] == stale["id"]

    def test_noop_sweep_commits_nothing(self, store: MemoryStore):
        store.write("tier2 decay noop", type="fact", source="agent-a")  # fact 永不衰减
        before = commit_count(store)
        assert store.decay_sweep() == []
        assert commit_count(store) == before


class TestReviewQueue:
    def test_lines_without_prefix(self, store: MemoryStore):
        store.write("tier2 queue old", type="fact", source="agent-a", key="q1")
        store.write("tier2 queue new", type="fact", source="agent-a", key="q1")
        (line,) = store.review_queue()
        assert line.startswith("20") and not line.startswith("- ")

    def test_empty_queue_returns_empty_list(self, store: MemoryStore):
        assert store.review_queue() == []


class TestRebuildIndex:
    def test_counts_shape_and_search_survives(self, store: MemoryStore):
        store.write("tier2 rebuild probe", type="fact", source="agent-a")
        counts = store.rebuild_index()
        assert counts["memories"] == 1
        assert "tokens" in counts
        assert [h["id"] for h in store.search("rebuild probe")] != []


class TestStats:
    def test_key_set_pinned(self, store: MemoryStore):
        stats = store.stats()
        assert set(stats) == {
            "total", "archived", "active", "avg_confidence", "by_type", "by_ns",
            "review_queue_entries", "uses_histogram", "confidence_histogram",
            "recent_feedback_7d", "cross_validated", "distilled_total",
            "distilled_recent_7d", "expired_active",
        }

    def test_basic_counts(self, store: MemoryStore):
        store.write("tier2 stats fact", type="fact", source="agent-a")
        store.write("tier2 stats episode", type="episode", source="agent-a", created=days_ago(120))
        store.decay_sweep()
        stats = store.stats()
        assert (stats["total"], stats["archived"], stats["active"]) == (2, 1, 1)


class TestGitLog:
    def test_returns_hash_prefixed_lines_respecting_limit(self, store: MemoryStore):
        store.write("tier2 gitlog probe", type="fact", source="agent-a")
        log = store.git_log(1)
        assert len(log) == 1
        # --oneline 形态：hash 前缀 + 消息
        assert subprocess.run(
            ["git", "-C", str(store.root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip() in log[0]

    def test_git_disabled_returns_empty(self, tmp_path: Path):
        store = MemoryStore(tmp_path / "nogit", git_probe=lambda: False)
        assert store.git_log() == []


class TestLexicalCandidates:
    """公开词面候选通道：与 search 同一门禁（凡返回记忆正文必过身份门）。"""

    def test_shared_ok_without_identity(self, store: MemoryStore):
        store.write("tier2 lexical shared", type="fact", source=FOREIGN)
        hits = store.lexical_candidates(tokenize("lexical shared"), {"_shared"})
        assert [m.id for m in hits]

    def test_private_ns_requires_owner(self, store: MemoryStore):
        priv = store.write("tier2 lexical priv", type="fact", source=OWNER, ns=PRIVATE_NS)
        with pytest.raises(PermissionError):
            store.lexical_candidates(tokenize("lexical priv"), {PRIVATE_NS})
        hits = store.lexical_candidates(tokenize("lexical priv"), {PRIVATE_NS}, reader=OWNER)
        assert [m.id for m in hits] == [priv["id"]]
        assert hits[0].content == "tier2 lexical priv"  # 返回正文，故必须过门

    def test_illegal_ns_is_caller_error(self, store: MemoryStore):
        with pytest.raises(ValueError):
            store.lexical_candidates(tokenize("x"), {"not-a-ns"})
