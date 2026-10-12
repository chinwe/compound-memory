"""dream 只读编排（#69）：报告契约 + 空跑不变量。

夹具含同 key 双版本与一条 valid_until 已过的记忆。默认路径不得写盘、
不得调用 distill_apply / forget / decay_sweep / feedback。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from compound_memory.cli import build_parser
from compound_memory.cli import main as cli_main
from compound_memory.storage import MemoryStore
from conftest import CLOCK_DATE


def _days_ago(n: int) -> str:
    """相对测试固定「今天」（CLOCK_DATE）推算——store fixture 的 clock 是同一天。"""
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


def _commit_count(root: Path) -> int:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def _payload_files(root: Path) -> dict[str, bytes]:
    """记忆库文件字节（不含 .git）。空跑前后必须逐字节相同。"""
    payload: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if ".git" in rel.parts:
            continue
        payload[rel.as_posix()] = path.read_bytes()
    return payload


def _seed(store: MemoryStore) -> dict[str, str]:
    """同 key 双版本 + valid_until 已过 + 一条低活性 + 一条已归档。

    另在私有 ns 放一条已过记忆，用来确认 _shared 的计数不串 ns。
    """
    older = store.write(
        content="proj-x 的旧版结论",
        type="fact",
        source="agent-a",
        key="proj-x",
        created=_days_ago(2),
    )
    newer = store.write(
        content="proj-x 的新版结论",
        type="fact",
        source="agent-a",
        key="proj-x",
        created=_days_ago(1),
    )
    expired = store.write(
        content="已经过了 valid_until 的事实",
        type="fact",
        source="agent-a",
        key="old-policy",
        valid_until=_days_ago(1),
    )
    store.write(
        content="长期未用的经历",
        type="episode",
        source="agent-a",
        created=_days_ago(120),
    )
    doomed = store.write(content="明确作废的经历", type="episode", source="agent-a")
    store.feedback(doomed["id"], "agent-a", outcome="obsolete")
    store.write(
        content="私有 ns 里已过的事实",
        type="fact",
        source="agent-zcode",
        ns="agent-zcode",
        valid_until=_days_ago(1),
    )
    return {"older": older["id"], "newer": newer["id"], "expired": expired["id"]}


class TestDreamReport:
    def test_key_duplicates_and_expiry_leave_store_unchanged(self, store: MemoryStore):
        ids = _seed(store)
        before_files = _payload_files(store.root)
        before_commits = _commit_count(store.root)

        report = store.dream(ns="_shared")

        assert report["ns"] == "_shared"
        assert report["dry_run"] is True
        assert set(report) == {
            "ns",
            "dry_run",
            "summary",
            "distill",
            "decay_signals",
            "stats_before",
            "stats_after",
        }
        assert report["distill"] == store.distill_plan(ns="_shared")
        groups = report["distill"]["key_duplicates"]
        assert len(groups) == 1
        assert groups[0]["key"] == "proj-x"
        assert groups[0]["type"] == "fact"
        assert {member["id"] for member in groups[0]["members"]} == {ids["older"], ids["newer"]}
        signals = report["decay_signals"]
        assert set(signals) == {"expired_count", "archived_count", "low_activity_count"}
        assert signals["expired_count"] >= 1
        assert signals["expired_count"] == 1
        assert signals["archived_count"] == 1
        assert signals["low_activity_count"] == 1
        assert report["stats_before"] == report["stats_after"] == store.stats()
        assert "dry-run" in report["summary"]
        assert "expired=1" in report["summary"]
        assert ids["expired"] not in {member["id"] for group in groups for member in group["members"]}

        assert _commit_count(store.root) == before_commits
        assert _payload_files(store.root) == before_files

    def test_forwards_distill_plan_parameters(self, store: MemoryStore):
        store.write(content="窗口内经历", type="episode", source="agent-a")
        report = store.dream(window_days=7, min_uses=0, min_confidence=0.1, ns="_shared")
        assert report["distill"] == store.distill_plan(
            window_days=7, min_uses=0, min_confidence=0.1, ns="_shared"
        )

    def test_private_ns_requires_owner(self, store: MemoryStore):
        store.write(
            content="私有经历",
            type="episode",
            source="agent-zcode",
            ns="agent-zcode",
        )
        with pytest.raises(PermissionError):
            store.dream(ns="agent-zcode")
        report = store.dream(ns="agent-zcode", reader="zcode")
        assert report["ns"] == "agent-zcode"
        assert report["dry_run"] is True
        assert report["stats_before"] == report["stats_after"]

    def test_invalid_ns_raises(self, store: MemoryStore):
        with pytest.raises(ValueError):
            store.dream(ns="not a ns")

    def test_default_path_does_not_call_write_verbs(self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch):
        store.write(content="只读探针", type="episode", source="agent-a")

        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("dream invoked a write verb")

        for name in ("write", "feedback", "decay_sweep", "distill_apply", "forget", "revive", "_archive", "_commit"):
            monkeypatch.setattr(store, name, boom)
        report = store.dream(ns="_shared")
        assert report["dry_run"] is True
        assert report["stats_before"] == report["stats_after"]


class TestDreamCli:
    def test_cli_dry_run_reports_duplicates_and_expiry(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        root = tmp_path / "dream-root"
        root_arg = str(root)

        def run(*argv: str) -> dict:
            assert cli_main(["--root", root_arg, *argv]) == 0
            return json.loads(capsys.readouterr().out)

        run("write", "proj-x 旧版", "fact", "agent-cli", "--key", "proj-x")
        run("write", "proj-x 新版", "fact", "agent-cli", "--key", "proj-x")
        run("write", "早已失效的事实", "fact", "agent-cli", "--key", "stale", "--valid-until", "2000-01-01")
        before_files = _payload_files(root)
        before_commits = _commit_count(root)

        assert cli_main(["--root", root_arg, "dream", "--ns", "_shared"]) == 0
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        assert report["dry_run"] is True
        assert report["distill"]["key_duplicates"]
        assert report["distill"]["key_duplicates"][0]["key"] == "proj-x"
        assert report["decay_signals"]["expired_count"] >= 1
        assert report["stats_before"] == report["stats_after"]
        assert captured.err.strip() == report["summary"]
        assert _commit_count(root) == before_commits
        assert _payload_files(root) == before_files

    def test_help_describes_read_only_report(self):
        action = next(
            item for item in build_parser()._actions if isinstance(item, argparse._SubParsersAction)
        )
        text = action.choices["dream"].format_help().lower()
        assert "dry-run" in text
        assert "read-only" in text
        assert "distill" in text
        assert "forget" in text
