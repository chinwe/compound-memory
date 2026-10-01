"""Tests at the CLI / store seam: decay, archive, revive, index cache, git, stats.

MCP 工具行为在 test_mcp_tools.py（MCP tool 边界缝）覆盖；
本文件覆盖不经过 MCP 暴露的运维面：decay_sweep / revive / rebuild_index / stats / git。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

import pytest

from compound_memory.cli import main as cli_main
from compound_memory.storage import MemoryStore


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memroot")


def _days_ago(n: int) -> str:
    return (dt.date.today() - dt.timedelta(days=n)).isoformat()


class TestDecayAndArchive:
    def test_stale_low_use_episode_gets_archived(self, store: MemoryStore):
        stale = store.write(content="旧的部署经验", type="episode", source="agent-a", created=_days_ago(120))
        fresh = store.write(content="新的部署经验", type="episode", source="agent-a")
        archived = store.decay_sweep()
        assert stale["id"] in archived
        assert fresh["id"] not in archived

    def test_heavily_used_episode_survives_ttl(self, store: MemoryStore):
        used = store.write(content="高频使用的经验", type="episode", source="agent-a", created=_days_ago(120))
        for agent in ("a", "b", "c"):
            store.feedback(used["id"], agent)
        archived = store.decay_sweep()
        assert used["id"] not in archived

    def test_fact_never_archived(self, store: MemoryStore):
        fact = store.write(content="长期事实", type="fact", source="agent-a", key="env", created=_days_ago(400))
        assert store.decay_sweep() == []

    def test_archived_memory_still_gettable_but_not_searchable(self, store: MemoryStore):
        mem = store.write(content="Vercel 超时限制是 10 秒", type="episode", source="agent-a", created=_days_ago(120))
        store.decay_sweep()
        got = store.get(mem["id"])
        assert got["found"] if "found" in got else got["archived"] is True
        assert got["archived"] is True
        assert store.search("Vercel 超时") == []

    def test_recently_used_survives_despite_old_created(self, store: MemoryStore):
        """spec: 衰减看'长期未用'——last_used 新则不归档."""
        mem = store.write(content="最近用过的老经验", type="episode", source="agent-a", created=_days_ago(120))
        store.feedback(mem["id"], "agent-a")  # last_used = today
        assert store.decay_sweep() == []

    def test_revive_restores_searchability(self, store: MemoryStore):
        mem = store.write(content="Next.js 静态导出经验", type="episode", source="agent-a", created=_days_ago(120))
        store.decay_sweep()
        revived = store.revive(mem["id"])
        assert revived["archived"] is False
        hits = store.search("Next.js 静态导出")
        assert [h["id"] for h in hits] == [mem["id"]]

    def test_feedback_on_archived_revives_it(self, store: MemoryStore):
        mem = store.write(content="被再次用到的旧经验", type="episode", source="agent-a", created=_days_ago(120))
        store.decay_sweep()
        result = store.feedback(mem["id"], "agent-b")
        assert result["archived"] is False
        assert result["uses"] == 1


class TestIndexCache:
    def test_search_works_without_index_and_with_index(self, store: MemoryStore):
        a = store.write(content="Python GIL 基础知识", type="episode", source="agent-a")
        store.write(content="Go goroutine 并发知识", type="episode", source="agent-a")
        scan_hits = store.search("Python GIL")
        assert [h["id"] for h in scan_hits] == [a["id"]]
        store.rebuild_index()
        index_hits = store.search("Python GIL")
        assert [h["id"] for h in index_hits] == [a["id"]]

    def test_search_survives_index_deletion(self, store: MemoryStore):
        mem = store.write(content="Docker 网络模式 bridge", type="episode", source="agent-a")
        assert store.index_file.exists()
        os.replace(store.index_file, store.index_file.with_name("tokens.json.deleted"))
        hits = store.search("Docker bridge")
        assert [h["id"] for h in hits] == [mem["id"]]


class TestGit:
    def test_write_and_feedback_create_commits(self, store: MemoryStore):
        mem = store.write(content="git 测试记忆", type="episode", source="agent-a")
        log1 = store.git_log(20)
        assert any("write" in line for line in log1)
        store.feedback(mem["id"], "agent-b")
        log2 = store.git_log(20)
        assert any("feedback" in line for line in log2)
        assert len(log2) > len(log1)

    def test_no_git_binary_disables_gracefully(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)
        store = MemoryStore(tmp_path / "nogit")
        mem = store.write(content="无 git 环境", type="episode", source="agent-a")
        assert mem["id"]
        assert store.search("无 git") != []


class TestStats:
    def test_stats_counts(self, store: MemoryStore):
        store.write(content="事实一", type="fact", source="agent-a", key="f1")
        store.write(content="经验一", type="episode", source="agent-a")
        store.write(content="私有草稿", type="episode", source="agent-tars", ns="agent-tars")
        stats = store.stats()
        assert stats["total"] == 3
        assert stats["by_type"]["episode"] == 2
        assert stats["by_ns"]["agent-tars"] == 1
        assert stats["review_queue_entries"] == 0


class TestCli:
    def test_cli_write_search_feedback_roundtrip(self, tmp_path: Path, capsys):
        root = tmp_path / "cliroot"
        assert cli_main(["--root", str(root), "write", "Rust 所有权规则", "episode", "agent-cli"]) == 0
        out = json.loads(capsys.readouterr().out)
        mem_id = out["id"]

        assert cli_main(["--root", str(root), "search", "Rust 所有权"]) == 0
        hits = json.loads(capsys.readouterr().out)
        assert [h["id"] for h in hits] == [mem_id]

        assert cli_main(["--root", str(root), "feedback", mem_id, "agent-cli"]) == 0
        got = json.loads(capsys.readouterr().out)
        assert got["uses"] == 1
        assert got["confidence"] == pytest.approx(0.6)

    def test_cli_rejects_bad_type(self, tmp_path: Path):
        with pytest.raises(SystemExit) as exc:
            cli_main(["--root", str(tmp_path / "r"), "write", "x", "bogus", "agent"])
        assert exc.value.code == 2
