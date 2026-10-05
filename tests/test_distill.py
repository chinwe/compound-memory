"""蒸馏管线测试：distill-plan 的窗口/活性门/双信号标注，distill-apply 的原子性与溯源。

确定性段（plan/apply）在 store + CLI 缝覆盖；判断段由调用方 Agent 完成，无代码可测。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import plistlib
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from compound_memory.cli import main as cli_main
from compound_memory.storage import MemoryStore
from conftest import CLOCK_DATE


def _days_ago(n: int) -> str:
    """相对测试固定"今天"（CLOCK_DATE）推算——store 的 clock 已注入同一日期。"""
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


def _commit_count(store: MemoryStore) -> int:
    proc = subprocess.run(
        ["git", "-C", str(store.root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


class TestDistillPlan:
    def test_window_filters_by_recency_reference(self, store: MemoryStore):
        """窗口按新近基准（last_used 优先，created 兜底）过滤（min_uses=0 隔离窗口变量）——
        老记忆被 feedback 救回窗口内；从未用过的老记忆留给衰减，不进蒸馏。"""
        old = store.write(content="三个月前的旧经验", type="episode", source="agent-a", created=_days_ago(100))
        fresh = store.write(content="最近的新经验", type="episode", source="agent-a", created=_days_ago(10))
        rescued = store.write(content="老但最近用过", type="episode", source="agent-a", created=_days_ago(100))
        store.feedback(rescued["id"], "agent-b")
        plan = store.distill_plan(window_days=30, min_uses=0)
        ids = [c["id"] for c in plan["candidates"]]
        assert fresh["id"] in ids
        assert rescued["id"] in ids
        assert old["id"] not in ids

    def test_activity_gate_excludes_dead_memories(self, store: MemoryStore):
        """活性门：uses=0 / confidence 不足的不进候选——蒸馏只针对被验证过的活记忆。"""
        dead = store.write(content="从未被使用的记忆", type="episode", source="agent-a")
        used = store.write(content="被使用过一次的记忆", type="episode", source="agent-a")
        store.feedback(used["id"], "agent-a")
        plan = store.distill_plan()
        ids = [c["id"] for c in plan["candidates"]]
        assert dead["id"] not in ids
        assert used["id"] in ids

    def test_confidence_gate(self, store: MemoryStore):
        low = store.write(content="单 Agent 验证的记忆", type="episode", source="agent-a")
        store.feedback(low["id"], "agent-a")  # conf 0.5 -> 0.6
        high = store.write(content="跨 Agent 验证的记忆", type="episode", source="agent-a")
        store.feedback(high["id"], "agent-b")  # conf 0.5 -> 0.75
        plan = store.distill_plan(min_confidence=0.7)
        ids = [c["id"] for c in plan["candidates"]]
        assert high["id"] in ids
        assert low["id"] not in ids

    def test_key_merge_signal_same_ns_type_key(self, store: MemoryStore):
        """key 强信号：同 ns 同 type 同 key 互标 merge_with——判断段据此合并。"""
        a = store.write(content="deploy 流程记录甲", type="episode", source="agent-a", key="deploy")
        b = store.write(content="deploy 流程记录乙", type="episode", source="agent-a", key="deploy")
        c = store.write(content="无关主题记忆", type="episode", source="agent-a", key="other")
        plan = store.distill_plan(min_uses=0)
        by_id = {c2["id"]: c2 for c2 in plan["candidates"]}
        assert by_id[a["id"]]["merge_with"] == [b["id"]]
        assert by_id[b["id"]]["merge_with"] == [a["id"]]
        assert by_id[c["id"]]["merge_with"] == []

    def test_possible_dup_signal_by_bm25(self, store: MemoryStore):
        """BM25 弱信号：内容高度相似互标 possible_dup_of；无关记忆不受牵连。"""
        dup_a = store.write(content="compound-memory 蒸馏管线把候选清单交给 agent 判断", type="episode", source="agent-a")
        dup_b = store.write(content="compound-memory 蒸馏管线把候选清单交给调用方判断", type="episode", source="agent-a")
        filler_ids = [
            store.write(content=title, type="episode", source="agent-a")["id"]
            for title in (
                "Python GIL 全局解释器锁行为",
                "Go goroutine 并发模型要点",
                "Redis 持久化 RDB 与 AOF 差异",
                "Kubernetes Pod 亲和性配置",
                "HTTP ETag 协商缓存机制",
                "Vercel Serverless 函数超时",
            )
        ]
        plan = store.distill_plan(min_uses=0)
        by_id = {c["id"]: c for c in plan["candidates"]}
        assert dup_b["id"] in by_id[dup_a["id"]]["possible_dup_of"]
        assert dup_a["id"] in by_id[dup_b["id"]]["possible_dup_of"]
        assert all(by_id[fid]["possible_dup_of"] == [] for fid in filler_ids)

    def test_promotion_signal_only_for_active_episodes(self, store: MemoryStore):
        """晋升建议（#6）：高活性 episode 标 promotion-candidate；fact 永不标
        （月固化产物走蒸馏新写，类型终身不变）。"""
        hot = store.write(content="反复验证的部署经验", type="episode", source="agent-a")
        for agent in ("a", "b", "c", "d", "e"):
            store.feedback(hot["id"], agent)  # uses=5
        fact = store.write(content="高频引用的事实", type="fact", source="agent-a", key="hot-fact")
        for agent in ("a", "b", "c", "d", "e"):
            store.feedback(fact["id"], agent)
        cold = store.write(content="只被用过一次的经验", type="episode", source="agent-a")
        store.feedback(cold["id"], "agent-a")
        plan = store.distill_plan()
        by_id = {c["id"]: c for c in plan["candidates"]}
        assert by_id[hot["id"]]["promotion_candidate"] is True
        assert by_id[fact["id"]]["promotion_candidate"] is False
        assert by_id[cold["id"]]["promotion_candidate"] is False

    def test_archived_memories_excluded(self, store: MemoryStore):
        stale = store.write(content="已归档的陈旧记忆", type="episode", source="agent-a", created=_days_ago(120))
        store.decay_sweep()
        assert store.distill_plan(min_uses=0, window_days=365)["candidates"] == []

    def test_invalid_ns_raises(self, store: MemoryStore):
        with pytest.raises(ValueError):
            store.distill_plan(ns="not-a-ns")


class TestDistillApply:
    def test_apply_is_atomic_write_links_archive_single_commit(self, store: MemoryStore):
        """原子落库：产物写入（links 溯源 + origin=distillation）+ 源归档，恰好一次 commit。"""
        src_a = store.write(content="零散经验片段甲", type="episode", source="agent-a")
        src_b = store.write(content="零散经验片段乙", type="episode", source="agent-a")
        store.feedback(src_a["id"], "agent-a")
        store.feedback(src_b["id"], "agent-a")
        before = _commit_count(store)

        result = store.distill_apply(
            content="合并后的部署经验", type="insight", source="agent-zcode", source_ids=[src_a["id"], src_b["id"]]
        )

        assert result["found"] is True
        assert result["conflict"] is False
        assert result["origin"] == "distillation"
        assert result["archived_sources"] == [src_a["id"], src_b["id"]]
        assert result["links"] == [src_a["id"], src_b["id"]]
        # 源已归档、产物可检索且带出源邻居
        assert store.get(src_a["id"])["archived"] is True
        assert store.get(src_b["id"])["archived"] is True
        product = store.get(result["id"])
        assert product["found"] is True and product["origin"] == "distillation"
        assert {n["id"] for n in product["neighbors"]} == {src_a["id"], src_b["id"]}
        assert _commit_count(store) - before == 1

    def test_apply_missing_source_leaves_store_untouched(self, store: MemoryStore):
        """任一源不存在 ⇒ 整体不落库（按 id 信封约定 found: False + missing）。"""
        src = store.write(content="唯一有效的源", type="episode", source="agent-a")
        before = _commit_count(store)
        result = store.distill_apply(content="x", type="insight", source="agent-a", source_ids=[src["id"], "nope"])
        assert result == {"found": False, "missing": ["nope"]}
        assert _commit_count(store) == before
        assert store.get(src["id"])["archived"] is False
        assert store.search("x") == []

    def test_apply_source_read_holds_write_lock(self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch):
        """源读取必须在写锁内（写动词读-改-写全程持锁的自查条款）：
        find 在锁外时，锁外间隙完成的并发 feedback 会被旧快照在归档
        写回时覆盖——uses/confidence 丢更新。"""
        src = store.write(content="待蒸馏的源经验", type="episode", source="agent-a")
        store.feedback(src["id"], "agent-b")  # uses=1 基线

        real_batch = store.batch

        @contextmanager
        def racing_batch(message=None):
            # 模拟锁外窗口完成的并发写动词：真实形态是另一进程抢在
            # distill_apply 拿锁前完成 feedback 落盘
            store.feedback(src["id"], "agent-c")
            with real_batch(message) as handle:
                yield handle

        monkeypatch.setattr(store, "batch", racing_batch)
        result = store.distill_apply(content="合并经验", type="insight", source="agent-a", source_ids=[src["id"]])
        monkeypatch.undo()

        assert result["found"] is True
        archived = store.find(src["id"])
        assert archived is not None and archived.archived
        # 并发反馈的 uses=2 不得被锁外旧快照（uses=1）在归档时覆盖
        assert archived.uses == 2

    def test_apply_key_conflict_uses_existing_review_queue(self, store: MemoryStore):
        """产物冲突不特殊对待：同 key 的 fact 冲突自然进 review 队列（既有机制）。"""
        existing = store.write(content="现行版本的部署事实", type="fact", source="agent-a", key="deploy")
        src = store.write(content="待蒸馏的部署记录", type="episode", source="agent-a")
        result = store.distill_apply(
            content="蒸馏出的另一版本部署事实",
            type="fact",
            source="agent-zcode",
            source_ids=[src["id"]],
            key="deploy",
        )
        assert result["conflict"] is True
        assert result["conflicts_with"] == existing["id"]
        assert len(store.review_queue()) == 1

    def test_apply_dedupes_and_skips_archived_sources(self, store: MemoryStore):
        """重复源只归档一次；已是归档态的源不重复搬运（复活交给既有自愈机制）。"""
        src = store.write(content="重复出现的源", type="episode", source="agent-a")
        result = store.distill_apply(
            content="产物", type="insight", source="agent-a", source_ids=[src["id"], src["id"]]
        )
        assert result["archived_sources"] == [src["id"]]
        assert store.get(src["id"])["archived"] is True

    def test_apply_default_confidence(self, store: MemoryStore):
        src = store.write(content="源记忆", type="episode", source="agent-a")
        result = store.distill_apply(content="产物", type="insight", source="agent-a", source_ids=[src["id"]])
        assert result["confidence"] == 0.5


class TestWriteOrigin:
    def test_origin_persisted_only_when_given(self, tmp_path):
        """origin 是可选 frontmatter 字段：普通写入不落盘，蒸馏通道写入才持久化。"""
        store = MemoryStore(tmp_path / "origin-root", clock=lambda: CLOCK_DATE)
        plain = store.write(content="普通写入", type="episode", source="agent-a")
        distilled = store.write(content="蒸馏写入", type="insight", source="agent-a", origin="distillation")
        assert "origin:" not in (store.ns_root / "_shared" / "episode" / f"{plain['id']}.md").read_text(encoding="utf-8")
        assert "origin: distillation" in (store.ns_root / "_shared" / "insight" / f"{distilled['id']}.md").read_text(encoding="utf-8")
        assert store.get(plain["id"])["origin"] is None
        assert store.get(distilled["id"])["origin"] == "distillation"


class TestCli:
    def test_cli_distill_plan_and_apply_roundtrip(self, tmp_path, capsys):
        """CLI 全链路：写源 → plan 出候选（含信号）→ apply 落库 → 源归档、产物可查。"""
        root = str(tmp_path / "cliroot")
        assert cli_main(["--root", root, "write", "零散经验片段甲", "episode", "agent-cli"]) == 0
        id_a = json.loads(capsys.readouterr().out)["id"]
        assert cli_main(["--root", root, "write", "零散经验片段乙", "episode", "agent-cli"]) == 0
        id_b = json.loads(capsys.readouterr().out)["id"]
        assert cli_main(["--root", root, "feedback", id_a, "agent-cli"]) == 0
        capsys.readouterr()
        assert cli_main(["--root", root, "feedback", id_b, "agent-cli"]) == 0
        capsys.readouterr()

        assert cli_main(["--root", root, "distill-plan"]) == 0
        plan = json.loads(capsys.readouterr().out)
        assert {c["id"] for c in plan["candidates"]} == {id_a, id_b}

        assert (
            cli_main(["--root", root, "distill-apply", "合并后的经验", "insight", "agent-cli", "--sources", f"{id_a}, {id_b}"])
            == 0
        )
        applied = json.loads(capsys.readouterr().out)
        assert applied["found"] is True
        assert applied["origin"] == "distillation"
        assert applied["archived_sources"] == [id_a, id_b]

        assert cli_main(["--root", root, "get", id_a]) == 0
        assert json.loads(capsys.readouterr().out)["archived"] is True

    def test_cli_distill_apply_reports_missing_sources(self, tmp_path, capsys):
        assert (
            cli_main(["--root", str(tmp_path / "m"), "distill-apply", "产物", "insight", "agent-cli", "--sources", "nope"]) == 0
        )
        out = json.loads(capsys.readouterr().out)
        assert out == {"found": False, "missing": ["nope"]}


def _uv_available() -> bool:
    """distill-prepare.sh 依赖 uv（PATH 或 ~/.local/bin）——都不在时明确跳过，而非费解报错。"""
    return shutil.which("uv") is not None or (Path.home() / ".local" / "bin" / "uv").exists()


class TestDistillPrepareScript:
    """蒸馏调度安装物（#9）：脚本产出 plan、plist 模板语法合法——坏了全量测试就红。"""

    @pytest.mark.skipif(not _uv_available(), reason="uv unavailable (not on PATH, no ~/.local/bin/uv)")
    def test_prepare_script_writes_plan_and_gitignores_output(self, tmp_path):
        """脚本把 plan 落到 <root>/distill/last-plan.json，且产物目录被 gitignore
        （.gitignore 由 store 的 _ensure_layout 统一管理，脚本不再自行补写）。"""
        repo = Path(__file__).resolve().parents[1]
        root = tmp_path / "script-root"
        env = {**os.environ, "COMPOUND_MEMORY_ROOT": str(root)}
        env.pop("PYTHONPATH", None)  # 脚本自己拼 PYTHONPATH，排除测试进程环境干扰
        proc = subprocess.run(
            ["sh", str(repo / "scripts" / "distill-prepare.sh")], env=env, capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr
        plan = json.loads((root / "distill" / "last-plan.json").read_text(encoding="utf-8"))
        assert plan["candidates"] == []
        assert "distill/" in (root / ".gitignore").read_text(encoding="utf-8")

    def test_launchagent_plist_template_is_valid(self, tmp_path):
        repo = Path(__file__).resolve().parents[1]
        text = (repo / "scripts" / "com.compound-memory.distill-prepare.plist.tmpl").read_text(encoding="utf-8")
        filled = (
            text.replace("__REPO__", str(repo))
            .replace("__PYTHON__", sys.executable)
            .replace("__ROOT__", str(tmp_path / "r"))
        )
        plist = tmp_path / "com.compound-memory.distill-prepare.plist"
        plist.write_text(filled, encoding="utf-8")
        # plistlib 跨平台校验语法；plutil 是 macOS 专属工具，CI 的 ubuntu runner 没有
        plistlib.loads(filled.encode("utf-8"))
