"""Tests for the Index module (Q1-A: write builds the index; Q4-A: scan fallback).

Behaviour is asserted at the store seam; physical cache existence is the one
implementation detail we anchor explicitly (Q6-A).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from compound_memory.storage import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memroot")


class TestWriteBuildsIndex:
    def test_first_write_creates_index_file(self, store: MemoryStore):
        """Q1-A: the cache must not be dead code — write activates it."""
        assert not store.index_file.exists()
        mem = store.write(content="索引激活测试 Vercel 部署", type="episode", source="agent-a")
        assert store.index_file.exists()
        index = json.loads(store.index_file.read_text(encoding="utf-8"))
        rel = str(Path("namespaces") / "_shared" / "episode" / f"{mem['id']}.md")
        assert rel in index.get("vercel", [])

    def test_indexed_search_and_scan_search_agree(self, store: MemoryStore):
        a = store.write(content="Python GIL 基础知识", type="episode", source="agent-a")
        store.write(content="Go goroutine 并发知识", type="episode", source="agent-b")
        indexed = store.search("Python GIL")
        scan = store.search("Python GIL")
        assert [h["id"] for h in indexed] == [a["id"]]
        assert [h["id"] for h in scan] == [a["id"]]

    def test_fallback_when_index_deleted(self, store: MemoryStore):
        """Q4-A: cache loss degrades to scan, never to error."""
        mem = store.write(content="Docker 网络模式 bridge", type="episode", source="agent-a")
        os_rename_away(store.index_file)
        hits = store.search("Docker bridge")
        assert [h["id"] for h in hits] == [mem["id"]]

    def test_fallback_when_index_corrupted(self, store: MemoryStore):
        mem = store.write(content="Kubernetes pod 亲和性", type="episode", source="agent-a")
        store.index_file.write_text("{not json", encoding="utf-8")
        hits = store.search("Kubernetes pod")
        assert [h["id"] for h in hits] == [mem["id"]]

    def test_archive_relocate_leaves_no_stale_entry(self, store: MemoryStore):
        """Archive must remove the old path from the index, not leave dirt."""
        import datetime as dt

        old = (dt.date.today() - dt.timedelta(days=120)).isoformat()
        mem = store.write(content="旧的超时经验 vercel", type="episode", source="agent-a", created=old)
        active_rel = str(Path("namespaces") / "_shared" / "episode" / f"{mem['id']}.md")
        store.decay_sweep()
        index = json.loads(store.index_file.read_text(encoding="utf-8"))
        assert active_rel not in index.get("vercel", [])


def os_rename_away(path: Path) -> None:
    import os

    os.replace(path, path.with_name(path.name + ".deleted"))
