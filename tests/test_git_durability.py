"""git 提交韧性：add 失败短路 / index.lock 有界重试 / 启动孤儿对账。

「落地即已提交」的恢复闭环（2026-10-05 审计后续）：文件落盘是真相源，
git 审计史跟随——commit 侧每个失败模式都必须有确定归宿，孤儿变更不许
被下一个写动词的 "write ..." 消息错位归因（2026-10-02 实测）。
storage 属敏感区，遵循仓库 TDD 约定：用例先于实现（先红后绿）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE, sandbox_safe_remove

# 与生产 index.lock 报文对齐的子串：重试判定按 stderr 含此串识别
LOCK_ERR = "fatal: Unable to create '.../index.lock': File exists."


def _make_store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memroot", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)


class TestAddFailureSkipsCommit:
    def test_add_failure_never_runs_commit(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """add 未完成就没有可提交的新内容：此时 commit 只会提交 staged 残留，
        消息与新变更错位归因（比审计空洞更误导）——必须短路，变更留工作树
        由启动对账收编。"""
        store = _make_store(tmp_path)
        calls: list[tuple[str, ...]] = []

        def fake_git(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(args)
            if args[0] == "add":
                return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="fatal: index locked")
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        store._git = fake_git
        store.write("orphan to be", type="fact", source="agent-a")
        assert "commit" not in [c[0] for c in calls]
        assert "git add failed" in capsys.readouterr().err


class TestIndexLockRetry:
    def test_transient_lock_on_add_is_retried(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """index.lock 瞬时冲突（flock 降级窗口 / 外部 git 进程竞争）：
        有界重试后恢复 ⇒ 正常入库，无告警、调用方无感。"""
        store = _make_store(tmp_path)
        attempts = {"add": 0}

        def fake_git(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[0] == "add":
                attempts["add"] += 1
                if attempts["add"] <= 2:
                    return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=LOCK_ERR)
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        store._git = fake_git
        store.write("retried fact", type="fact", source="agent-a")
        assert attempts["add"] == 3
        assert capsys.readouterr().err == ""

    def test_transient_lock_on_commit_is_retried(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        store = _make_store(tmp_path)
        attempts = {"commit": 0}

        def fake_git(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[0] == "commit":
                attempts["commit"] += 1
                if attempts["commit"] <= 2:
                    return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr=LOCK_ERR)
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        store._git = fake_git
        store.write("commit retry fact", type="fact", source="agent-a")
        assert attempts["commit"] == 3
        assert capsys.readouterr().err == ""

    def test_permanent_failure_is_not_retried(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """hook 拒绝等永久性错误不在重试范围：一次告警即止。"""
        store = _make_store(tmp_path)
        attempts = {"add": 0}

        def fake_git(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[0] == "add":
                attempts["add"] += 1
                return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="error: hook rejected")
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        store._git = fake_git
        store.write("hooked fact", type="fact", source="agent-a")
        assert attempts["add"] == 1
        assert "git add failed" in capsys.readouterr().err


class TestOrphanRecovery:
    @staticmethod
    def _break_commit(store: MemoryStore) -> None:
        """模拟 commit 侧彻底失败：文件落盘、审计史留洞。"""

        def fake_git(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[0] in ("add", "commit"):
                return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="fatal: disk full")
            return subprocess.CompletedProcess(args, returncode=0, stdout="", stderr="")

        store._git = fake_git

    def test_startup_recovers_orphan_changes(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """commit 失败留下的孤儿变更：下次进程启动收编进明确标注的恢复提交——
        不再被下一个写动词的消息错位归因（2026-10-02 实测的错位场景闭环）。"""
        store = _make_store(tmp_path)
        self._break_commit(store)
        mem = store.write("orphan fact", type="fact", source="agent-a")
        assert mem["id"]
        capsys.readouterr()  # 清掉失败告警，下面只看恢复路径的输出

        store2 = MemoryStore(store.root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        log = store2.git_log()
        assert "orphan changes recovered" in log[0]
        assert store2.find(mem["id"]) is not None  # 对账只收编审计史，不动文件

        # 幂等：工作树已干净，再次启动不再产生恢复提交
        store3 = MemoryStore(store.root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        assert store3.git_log() == log

    def test_clean_startup_makes_no_recovery_commit(self, tmp_path: Path) -> None:
        """干净工作树零副作用：正常库的每次启动不得多出提交。"""
        store = _make_store(tmp_path)
        store.write("settled fact", type="fact", source="agent-a")
        baseline = store.git_log()
        store2 = MemoryStore(store.root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        assert store2.git_log() == baseline

    def test_recover_degrades_on_broken_repo(self, tmp_path: Path) -> None:
        """status 失败（坏仓库）必须降级跳过对账，不炸 __init__——
        check=True 的 CalledProcessError 会让 CLI/MCP 完全无法启动，
        「启动路径宁降级」的注释必须是真的。坏仓库用 gitfile 指向
        不存在路径制造：git 报 fatal 且不向上逃逸（空 .git 目录会被
        git 的仓库发现向上「借」到父仓库——曾把测试机的未提交改动
        commit 进开发仓库，勿再用那种形态）。"""
        store = _make_store(tmp_path)
        mid = store.write("survivor fact", type="fact", source="agent-a")["id"]
        # .git 目录整体挪走，原位换成指向不存在路径的 gitfile
        (store.root / ".git").rename(store.root / ".git.broken")
        (store.root / ".git").write_text("gitdir: /definitely/not/a/repo\n", encoding="utf-8")
        store2 = MemoryStore(store.root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)  # 不得抛
        assert store2.find(mid) is not None  # 降级不动文件，读路径照常

    def test_recover_status_check_holds_lock(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """spec「进程启动时（store 构造、持写锁）对账工作树」：status 判定
        必须在锁内——锁外判定后、拿锁前的窗口内落盘的变更会漏出本次收编
        （TOCTOU，下次启动才兜底）。"""
        store = _make_store(tmp_path)
        real_git = MemoryStore._git
        observed: dict[str, int] = {}

        def probing_git(self: MemoryStore, *args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args and args[0] == "status":
                observed["depth"] = self._write_lock_depth
            return real_git(self, *args, **kwargs)

        monkeypatch.setattr(MemoryStore, "_git", probing_git)
        MemoryStore(store.root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        monkeypatch.undo()
        assert observed.get("depth", 0) > 0
