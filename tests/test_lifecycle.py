"""Tests at the CLI / store seam: decay, archive, revive, index cache, git, stats.

MCP 工具行为在 test_mcp_tools.py（MCP tool 边界缝）覆盖；
本文件覆盖不经过 MCP 暴露的运维面：decay_sweep / revive / rebuild_index / stats / git。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from compound_memory.cli import main as cli_main
from compound_memory.storage import MemoryStore, _conf_bucket, _uses_bucket
from conftest import CLOCK_DATE


def _days_ago(n: int) -> str:
    """相对测试固定"今天"（CLOCK_DATE）推算——store 的 clock 已注入同一日期。"""
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


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
        assert got["found"] is True
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


    def test_bad_last_used_date_skipped_not_fatal(self, store: MemoryStore):
        """手编坏日期（spec story 10 鼓励直接编辑文件）不得杀掉整场 decay 扫描：
        坏日期的记忆跳过（宁可不归档，不因坏数据丢记忆），其余记忆照常处理。"""
        bad = store.write(content="被手编坏日期的记忆", type="episode", source="agent-a", created=_days_ago(120))
        path = store.ns_root / "_shared" / "episode" / f"{bad['id']}.md"
        text = path.read_text(encoding="utf-8").replace("created:", "last_used: not-a-date\ncreated:", 1)
        path.write_text(text, encoding="utf-8")
        good = store.write(content="正常的陈旧记忆", type="episode", source="agent-a", created=_days_ago(120))
        archived = store.decay_sweep()
        assert bad["id"] not in archived
        assert good["id"] in archived


class TestIndexCache:
    def test_rebuild_index_reports_counts_and_keeps_search(self, store: MemoryStore):
        """显式重建动词（运维面）：报告计数，且重建后检索如常。"""
        a = store.write(content="Python GIL 基础知识", type="episode", source="agent-a")
        store.write(content="Go goroutine 并发知识", type="episode", source="agent-a")
        counts = store.rebuild_index()
        assert counts["memories"] == 2
        assert [h["id"] for h in store.search("Python GIL")] == [a["id"]]


class TestRemovalAdapter:
    def test_default_remover_unlinks_in_production(self, tmp_path: Path):
        """生产默认 adapter 是真删除：单文件 unlink 不受沙箱批量守卫影响，
        归档/复活不在生产库留 .rm 尸体（改名式 remover 只存在于测试侧）。"""
        store = MemoryStore(tmp_path / "prodroot")
        mem = store.write(content="将被归档的记忆", type="episode", source="agent-a", created=_days_ago(120))
        store.decay_sweep()
        active_dir = store.ns_root / "_shared" / "episode"
        assert not (active_dir / f"{mem['id']}.md").exists()
        assert not list(active_dir.glob("*.rm"))


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

    def test_stats_health_extension(self, store: MemoryStore):
        """健康度扩展（story 20）：固定桶分布 + 复利活性两项 + 蒸馏产出量；
        既有键不动，新键只增不改。"""
        dead = store.write(content="死本金记忆", type="episode", source="agent-a")  # uses=0
        used = store.write(content="单 Agent 用过", type="episode", source="agent-a")
        store.feedback(used["id"], "agent-a")  # uses=1, conf=0.6
        cross = store.write(content="跨 Agent 验证", type="episode", source="agent-a")
        store.feedback(cross["id"], "agent-a")
        store.feedback(cross["id"], "agent-b")  # uses=2, conf=0.85, validated_by 2 人
        old_distilled = store.write(content="十天前的蒸馏产物", type="insight", source="agent-zcode",
                                     origin="distillation", created=_days_ago(10))
        fresh_distilled = store.write(content="今天的蒸馏产物", type="insight", source="agent-zcode",
                                      origin="distillation")
        stats = store.stats()
        assert stats["uses_histogram"] == {"0": 3, "1-2": 2, "3-5": 0, "6-9": 0, "10+": 0}
        assert stats["confidence_histogram"] == {"<0.3": 0, "0.3-0.6": 3, "0.6-0.8": 1, "0.8-1.0": 1}
        assert stats["recent_feedback_7d"] == 2  # used/cross 今天被 feedback；dead 从未用过
        assert stats["cross_validated"] == 1
        assert stats["distilled_total"] == 2
        assert stats["distilled_recent_7d"] == 1  # 老产物 created 超窗
        # 既有键不受影响
        assert stats["total"] == 5
        assert stats["by_type"]["insight"] == 2

    def test_stats_archived_distillation_still_counted(self, store: MemoryStore):
        """产出量统计覆盖归档区——蒸馏产出是历史事实，不因源/产物归档而消失。"""
        product = store.write(content="将被归档的产物", type="insight", source="agent-zcode", origin="distillation")
        store._archive(store.find(product["id"]))  # type: ignore[arg-type]
        stats = store.stats()
        assert stats["distilled_total"] == 1
        assert stats["distilled_recent_7d"] == 1

    def test_stats_bucket_boundaries(self):
        """桶边界单点验证：uses 以 3（归档存活线）分桶，confidence 以 0.3/0.6/0.8 分桶。"""
        assert [_uses_bucket(u) for u in (0, 1, 2, 3, 5, 6, 9, 10, 99)] == [
            "0", "1-2", "1-2", "3-5", "3-5", "6-9", "6-9", "10+", "10+"
        ]
        assert [_conf_bucket(c) for c in (0.0, 0.29, 0.3, 0.59, 0.6, 0.79, 0.8, 1.0)] == [
            "<0.3", "<0.3", "0.3-0.6", "0.3-0.6", "0.6-0.8", "0.6-0.8", "0.8-1.0", "0.8-1.0"
        ]


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

    def test_cli_permission_error_returns_json_not_traceback(self, tmp_path: Path, capsys):
        """接口错误约定的 CLI 侧：越权写 agent-* 命名空间必须翻译成 JSON + exit 2，
        而不是裸栈崩溃（旧实现只捕 ValueError，此路径直接 traceback）。"""
        code = cli_main(["--root", str(tmp_path / "p"), "write", "x", "episode", "agent-a", "--ns", "agent-tars"])
        assert code == 2
        err = json.loads(capsys.readouterr().err)
        assert "private" in err["error"]

    def test_cli_link_reports_missing_ids(self, tmp_path: Path, capsys):
        """按 id 动词的信封约定：不存在 = {"found": false}，不是异常也不是第三种键名。"""
        assert cli_main(["--root", str(tmp_path / "l"), "link", "nope", "alsono"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"found": False, "missing": ["nope", "alsono"]}

    def test_cli_search_neighbors_toggle(self, tmp_path: Path, capsys):
        """search 默认内嵌邻居；--no-neighbors 关闭（hits 不带 neighbors 键）。"""
        root = str(tmp_path / "sroot")
        assert cli_main(["--root", root, "write", "锚点记忆内容", "episode", "agent-cli"]) == 0
        anchor = json.loads(capsys.readouterr().out)["id"]
        assert cli_main(["--root", root, "write", "外围邻居内容", "episode", "agent-cli"]) == 0
        side = json.loads(capsys.readouterr().out)["id"]
        assert cli_main(["--root", root, "link", anchor, side]) == 0
        capsys.readouterr()

        assert cli_main(["--root", root, "search", "锚点记忆"]) == 0
        hit = json.loads(capsys.readouterr().out)[0]
        assert [n["id"] for n in hit["neighbors"]] == [side]

        assert cli_main(["--root", root, "search", "锚点记忆", "--no-neighbors"]) == 0
        hit = json.loads(capsys.readouterr().out)[0]
        assert "neighbors" not in hit
