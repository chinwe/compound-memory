"""CLI 健康检查：未注入 COMPOUND_MEMORY_AGENT_ID 时 stats / doctor 退出码 1。

诊断仍打印（stdout JSON，告警在 stderr）。MCP 五个工具不在这里重测——
契约钉在 tests/contracts/test_mcp_surface.py；存储层 stats() 不因身份缺失抛错。
词面降级与脏工作树只报告，不因此失败。其它硬失败（根目录缺失、写锁、git）
在 AGENT_ID 已设置时仍可让 doctor 退出 1。

另钉 health-snapshot / health-trend（#100 健康时序）：snapshot 追加
date + stats 原样行（不持锁、同日多行、health/ 不入 git）；trend 读面
现算每日末条与首尾 delta，空态退出码 0。两动词均不进 MCP 面。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from compound_memory import cli as cli_mod
from compound_memory.storage import MemoryStore
from compound_memory.storage.validation import ATTESTATION_WARNING

from conftest import CLOCK_DATE, sandbox_safe_remove


def _run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, Any, str]:
    """端到端跑 main()。拦截 auto_encoder，避免依赖本机 vec 模型。"""
    monkeypatch.setattr(cli_mod, "auto_encoder", lambda: None)
    rc = cli_mod.main(list(argv))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return rc, payload, captured.err


def _init_store(tmp_path: Path) -> Path:
    root = tmp_path / "memroot"
    MemoryStore(root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
    return root


@pytest.fixture(autouse=True)
def _clear_agent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPOUND_MEMORY_AGENT_ID", raising=False)


class TestStatsExitCode:
    def test_unset_agent_id_exits_nonzero_and_still_prints_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        rc, payload, err = _run(monkeypatch, capsys, "--root", str(root), "stats")
        assert rc == 1
        assert payload["attestation"]["attested"] is False
        assert payload["attestation"]["warning"] == ATTESTATION_WARNING
        assert "uses_histogram" in payload
        assert ATTESTATION_WARNING in err
        assert "CLI stats and doctor exit 1" in err

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_agent_id_exits_nonzero(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        blank: str,
    ):
        root = _init_store(tmp_path)
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", blank)
        rc, payload, err = _run(monkeypatch, capsys, "--root", str(root), "stats")
        assert rc == 1
        assert payload["attestation"]["attested"] is False
        assert payload["attestation"]["agent_id"] is None
        assert ATTESTATION_WARNING in err

    def test_set_agent_id_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
        rc, payload, err = _run(monkeypatch, capsys, "--root", str(root), "stats")
        assert rc == 0
        assert payload["attestation"] == {
            "attested": True,
            "agent_id": "agent-cursor",
            "warning": None,
        }
        assert ATTESTATION_WARNING not in err
        # 词面降级只是 stderr 注记，不把已注入身份的 stats 打成失败。
        assert payload["retrieval"]["mode"] == "lexical"
        assert "retrieval is lexical-only" in err

    def test_help_documents_exit_code(self, capsys: pytest.CaptureFixture[str]):
        with pytest.raises(SystemExit) as caught:
            cli_mod.main(["stats", "--help"])
        assert caught.value.code == 0
        text = capsys.readouterr().out
        assert "Exit 1" in text
        assert "COMPOUND_MEMORY_AGENT_ID" in text


class TestDoctor:
    def test_unset_agent_id_fails_with_diagnosis(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        rc, report, err = _run(monkeypatch, capsys, "--root", str(root), "doctor")
        assert rc == 1
        assert report["ok"] is False
        assert report["problems"] == ["attestation"]
        assert report["attestation"]["attested"] is False
        assert report["attestation"]["warning"] == ATTESTATION_WARNING
        assert report["root"]["status"] == "present"
        assert report["write_lock"]["status"] == "acquired"
        assert report["git"]["repo"] is True
        assert report["git"]["dirty"] is False
        assert report["retrieval"]["mode"] == "lexical"
        assert report["retrieval"]["reason"] == "embedder-unavailable"
        assert "retrieval" not in report["problems"]
        assert ATTESTATION_WARNING in err
        assert "doctor: fail (attestation)" in err

    def test_set_agent_id_exits_zero_on_healthy_store(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "  agent-cursor  ")
        rc, report, err = _run(monkeypatch, capsys, "--root", str(root), "doctor")
        assert rc == 0
        assert report["ok"] is True
        assert report["problems"] == []
        assert report["attestation"]["attested"] is True
        assert report["attestation"]["agent_id"] == "agent-cursor"
        assert report["write_lock"] == {
            "ok": True,
            "status": "acquired",
            "detail": None,
            "skipped": False,
        }
        assert report["git"]["repo"] is True
        assert report["git"]["dirty"] is False
        assert report["retrieval"]["mode"] == "lexical"
        assert "doctor: ok" in err
        assert ATTESTATION_WARNING not in err

    def test_missing_root_fails_without_creating_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = tmp_path / "missing-store"
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
        rc, report, err = _run(monkeypatch, capsys, "--root", str(root), "doctor")
        assert rc == 1
        assert report["problems"] == ["root"]
        assert report["root"]["status"] == "missing"
        assert report["write_lock"]["status"] == "skipped"
        assert report["git"]["skipped"] is True
        assert report["retrieval"]["reason"] == "skipped: root-missing"
        assert not root.exists()
        assert "store root missing:" in err
        assert "doctor: fail (root)" in err

    def test_not_a_directory_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = tmp_path / "not-a-dir"
        root.write_text("x", encoding="utf-8")
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
        rc, report, _err = _run(monkeypatch, capsys, "--root", str(root), "doctor")
        assert rc == 1
        assert report["problems"] == ["root"]
        assert report["root"]["status"] == "not-a-directory"

    def test_missing_git_repo_is_hard_fail_and_does_not_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = tmp_path / "empty"
        root.mkdir()
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
        rc, report, err = _run(monkeypatch, capsys, "--root", str(root), "doctor")
        assert rc == 1
        assert report["problems"] == ["git"]
        assert report["git"]["reason"] == "not-a-repository"
        assert report["retrieval"]["reason"] == "skipped: not-a-repository"
        assert not (root / ".git").exists()
        assert "git: not-a-repository" in err

    def test_dirty_worktree_is_reported_but_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        (root / "stray.txt").write_text("uncommitted", encoding="utf-8")
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
        rc, report, err = _run(monkeypatch, capsys, "--root", str(root), "doctor")
        assert rc == 0
        assert report["ok"] is True
        assert "git" not in report["problems"]
        assert report["git"]["dirty"] is True
        assert report["git"]["reason"] == "dirty-worktree"
        assert "git worktree is dirty" in err

    def test_git_unavailable_fails_even_with_agent_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
        monkeypatch.setattr("compound_memory.storage.gitlayer._git_available", lambda: False)
        rc, report, err = _run(monkeypatch, capsys, "--root", str(root), "doctor")
        assert rc == 1
        assert report["problems"] == ["git"]
        assert report["git"]["reason"] == "git-unavailable"
        assert report["attestation"]["attested"] is True
        assert "git: git-unavailable" in err

    def test_help_documents_exit_code(self, capsys: pytest.CaptureFixture[str]):
        with pytest.raises(SystemExit) as caught:
            cli_mod.main(["doctor", "--help"])
        assert caught.value.code == 0
        text = capsys.readouterr().out
        assert "Exit 1" in text
        assert "Exit 0" in text
        assert "COMPOUND_MEMORY_AGENT_ID" in text
        assert "dirty worktree" in text


def _write_health_lines(root: Path, records: list[dict[str, Any]]) -> None:
    """手写 health.jsonl（trend 读面测试播种；行 = date + stats dict 原样）。"""
    out_dir = root / "health"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "health.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8"
    )


def _stats_line(date: str, **overrides: Any) -> dict[str, Any]:
    """最小 stats 行：trend 直读键全给默认，测试里按需覆盖。"""
    base: dict[str, Any] = {
        "total": 3,
        "archived": 1,
        "avg_confidence": 0.5,
        "review_queue_entries": 1,
        "distilled_recent_7d": 0,
        "cross_validated": 1,
        "recent_feedback_7d": 1,
    }
    base.update(overrides)
    return {"date": date, "stats": base}


class TestHealthSnapshot:
    """health-snapshot：追加一行 date + stats 原样；单写者不持锁；不入 git。"""

    def test_appends_one_line_with_date_and_verbatim_stats(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
        rc, payload, _err = _run(
            monkeypatch, capsys, "--root", str(root), "health-snapshot", "--now", "2026-10-01"
        )
        assert rc == 0
        assert payload["ok"] is True
        lines = (root / "health" / "health.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        # 行 = date + stats dict 原样（指标单一来源是 stats 动词，不另算）
        assert record["date"] == "2026-10-01"
        assert record["stats"]["total"] == 0
        assert "uses_histogram" in record["stats"]
        assert record["stats"] == payload["snapshot"]

    def test_repeated_calls_append_multiple_lines_no_write_side_dedup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
        for _ in range(3):
            rc, _payload, _err = _run(
                monkeypatch, capsys, "--root", str(root), "health-snapshot", "--now", "2026-10-01"
            )
            assert rc == 0
        lines = (root / "health" / "health.jsonl").read_text(encoding="utf-8").splitlines()
        # 同日多次调用即多行——去重归读面，写侧不做
        assert len(lines) == 3

    def test_health_history_is_gitignored_not_swallowed_by_git(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
        rc, _payload, _err = _run(
            monkeypatch, capsys, "--root", str(root), "health-snapshot", "--now", "2026-10-01"
        )
        assert rc == 0
        # 布局初始化把 health/ 收进 .gitignore 清单（与 index/、extract/ 同型工件）
        gitignore = (root / ".gitignore").read_text(encoding="utf-8")
        assert "health/" in gitignore.splitlines()
        # 健康历史不被 git 提交吞走：工作树对 health/ 不可见
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "health/" not in status


class TestHealthTrend:
    """health-trend：读面现算——每日末条、首尾 delta、空态退出码 0。"""

    def test_missing_file_is_explicit_empty_state_exit_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        rc, report, err = _run(
            monkeypatch, capsys, "--root", str(root), "health-trend", "--days", "7",
            "--now", "2026-10-01",
        )
        assert rc == 0
        assert report["status"] == "empty"
        assert report["reason"] == "missing-file"
        assert report["rows"] == []
        assert report["delta"] is None
        assert "health-trend" in err

    def test_window_without_data_is_empty_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        _write_health_lines(root, [_stats_line("2026-01-01", total=9)])
        rc, report, _err = _run(
            monkeypatch, capsys, "--root", str(root), "health-trend", "--days", "7",
            "--now", "2026-10-01",
        )
        assert rc == 0
        assert report["status"] == "empty"
        assert report["reason"] == "no-snapshots-in-window"

    def test_daily_last_row_and_first_last_delta(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        _write_health_lines(
            root,
            [
                _stats_line("2026-09-28", total=3, avg_confidence=0.4, review_queue_entries=2),
                # 窗口外旧行不得出现
                _stats_line("2026-09-20", total=99),
                # 同日两行：读面取每日最后一条（文件序即追加序）
                _stats_line("2026-10-01", total=4),
                _stats_line("2026-10-01", total=5, avg_confidence=0.6, review_queue_entries=1),
            ],
        )
        rc, report, _err = _run(
            monkeypatch, capsys, "--root", str(root), "health-trend", "--days", "7",
            "--now", "2026-10-01",
        )
        assert rc == 0
        assert report["status"] == "ok"
        assert [row["date"] for row in report["rows"]] == ["2026-09-28", "2026-10-01"]
        assert report["rows"][-1]["total"] == 5  # 当日末条而非首条
        assert report["count"] == 2
        delta = report["delta"]
        assert delta["from"] == "2026-09-28"
        assert delta["to"] == "2026-10-01"
        assert delta["total"] == 2  # 5 - 3
        assert delta["avg_confidence"] == pytest.approx(0.2)
        assert delta["review_queue_entries"] == -1  # 1 - 2

    def test_rates_computed_at_read_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        # archived 1/4 与 cross_validated 2/4：比率读时现算，不落盘派生值
        _write_health_lines(root, [_stats_line("2026-10-01", total=4, archived=1, cross_validated=2)])
        rc, report, _err = _run(
            monkeypatch, capsys, "--root", str(root), "health-trend", "--days", "7",
            "--now", "2026-10-01",
        )
        assert rc == 0
        row = report["rows"][0]
        assert row["archive_rate"] == 0.25
        assert row["cross_validated_rate"] == 0.5

    def test_malformed_lines_are_skipped_and_counted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        _write_health_lines(root, [_stats_line("2026-10-01", total=7)])
        with (root / "health" / "health.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("not-json {{{\n")
            fh.write(json.dumps({"date": "2026-10-01"}) + "\n")  # 缺 stats 键
        rc, report, _err = _run(
            monkeypatch, capsys, "--root", str(root), "health-trend", "--days", "7",
            "--now", "2026-10-01",
        )
        assert rc == 0
        assert report["status"] == "ok"
        assert report["count"] == 1
        assert report["rows"][0]["total"] == 7
        assert report["skipped_lines"] == 2

    def test_days_below_one_is_caller_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        rc, payload, err = _run(monkeypatch, capsys, "--root", str(root), "health-trend", "--days", "0")
        assert rc == 2
        # 调用方错误统一翻译成 stderr JSON（stdout 保持空）
        assert payload is None
        assert "days" in json.loads(err)["error"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock holder; Windows lock path is msvcrt")
class TestDoctorLock:
    def test_contended_lock_fails_even_with_agent_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_store(tmp_path)
        lock_path = root / ".lock"
        sentinel = tmp_path / "held"
        holder = (
            "import fcntl, os, sys, time\n"
            "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
            "fcntl.flock(fd, fcntl.LOCK_EX)\n"
            "open(sys.argv[2], 'w').close()\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen([sys.executable, "-c", holder, str(lock_path), str(sentinel)])
        try:
            deadline = time.monotonic() + 15
            while not sentinel.exists():
                if proc.poll() is not None:
                    pytest.fail(f"holder exited {proc.returncode} before locking")
                if time.monotonic() > deadline:
                    pytest.fail("holder never acquired the lock within 15s")
                time.sleep(0.05)
            monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-cursor")
            rc, report, err = _run(monkeypatch, capsys, "--root", str(root), "doctor")
        finally:
            proc.kill()
            proc.wait(timeout=5)
        assert rc == 1
        assert report["problems"] == ["write_lock"]
        assert report["write_lock"]["status"] == "contended"
        assert report["attestation"]["attested"] is True
        assert report["retrieval"]["reason"] == "skipped: write-lock"
        assert "write lock:" in err
        assert "doctor: fail (write_lock)" in err
