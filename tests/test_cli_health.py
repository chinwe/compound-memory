"""CLI 健康检查：未注入 COMPOUND_MEMORY_AGENT_ID 时 stats / doctor 退出码 1。

诊断仍打印（stdout JSON，告警在 stderr）。MCP 五个工具不在这里重测——
契约钉在 tests/contracts/test_mcp_surface.py；存储层 stats() 不因身份缺失抛错。
词面降级与脏工作树只报告，不因此失败。其它硬失败（根目录缺失、写锁、git）
在 AGENT_ID 已设置时仍可让 doctor 退出 1。
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
