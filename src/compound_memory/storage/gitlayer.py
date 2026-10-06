"""git 子进程机制件（#36 片 d）：提交、有界重试、孤儿恢复、审计读取。

形态为 runner 注入式模块函数（facade 持有 git 状态并逐调用注入自身
`_git` 方法）：store._git 的实例级 stub / 类级 patch 是既有测试缝，
经参数注入可在调用时刻解析——stub 始终拦截得到全部子进程调用。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable

GIT_IDENTITY = ("-c", "user.name=compound-memory", "-c", "user.email=memory@local")
# index.lock 瞬时冲突的退避序列（retry）：初试 + 每档一次重试
GIT_LOCK_RETRY_DELAYS = (0.05, 0.2)

# _git runner：facade 的同名方法（*args + check=），注入以保住测试缝
GitRunner = Callable[..., subprocess.CompletedProcess[str]]
# 写锁提供者（recover 的 status 判定与收编须同临界区）
LockProvider = Callable[[], AbstractContextManager[None]]


def _git_available() -> bool:
    """默认 git 探测 adapter（测试侧经 git_probe 注入，勿 patch 全局 shutil.which）。"""
    return shutil.which("git") is not None


def git_env(root: Path) -> dict[str, str]:
    """git 子进程 env（仓库发现天花板，防逃逸）。"""
    # root 的 .git 无效（损坏/被清空）时 git 会跳过它继续向上、借父链最近的
    # 真仓库执行 add -A/commit（2026-10-05 实测把父仓库的未提交改动收编走）；
    # ceiling 钉在 root.parent，无效 .git 报 not a repository 而非逃逸。root 的
    # .git 有效时发现第一跳即命中，行为不变
    return {**os.environ, "GIT_CEILING_DIRECTORIES": os.path.realpath(root.parent)}


def run(root: Path, env: dict[str, str], args: tuple[str, ...], check: bool = True) -> subprocess.CompletedProcess[str]:
    """裸 git 子进程调用（facade._git 的实现体）。"""
    return subprocess.run(
        ["git", "-C", str(root), *GIT_IDENTITY, *args],
        capture_output=True,
        text=True,
        check=check,
        env=env,
    )


def retry(_git: GitRunner, *args: str) -> subprocess.CompletedProcess[str]:
    """index.lock 瞬时冲突的有界重试（check=False 语义不变，只多退避重试）。

    覆盖两类真实竞争：flock 降级窗口内的并发写者、外部 git 进程（手工
    操作/其他工具）。hook 拒绝等永久性错误不含 index.lock 字样，一次即
    返回——重试只该买瞬时冲突，不该烧时间在必然重现的失败上。
    """
    step = _git(*args, check=False)
    for delay in GIT_LOCK_RETRY_DELAYS:
        if step.returncode == 0 or "index.lock" not in (step.stderr or ""):
            break
        time.sleep(delay)
        step = _git(*args, check=False)
    return step


def init_commit(_git: GitRunner) -> None:
    """首次建库的初始提交三连（check=False：坏环境降级不炸启动路径）。"""
    _git("init", "-q", check=False)
    _git("add", "-A", check=False)
    _git("commit", "-qm", "init compound-memory store", check=False)


def commit(_git: GitRunner, message: str, *, enabled: bool, defer: Callable[[], bool]) -> None:
    """提交动词的收口实现：批式延迟经 defer 谓词（batch 状态归 locking 片），
    add 失败短路放弃（防错位归因），失败响亮告警。"""
    if defer():
        # 批内延迟：commit 收拢到 batch() 退出时一次性执行（单点拦截，各动词无需批式特化）
        return
    if not enabled:
        return
    staged = retry(_git, "add", "-A")
    combined = (staged.stdout or "") + (staged.stderr or "")
    if staged.returncode != 0:
        # add 未完成就没有可提交的新内容：commit 只会提交 staged 残留，
        # 消息与新变更错位归因（比审计空洞更误导）——短路放弃，变更留
        # 工作树由启动对账收编。check=False 的失败不得静默："git log 即
        # 审计史"的承诺至少要 stderr 响亮一声。
        if "nothing to commit" not in combined:
            print(f"compound-memory: git add failed: {combined.strip()}", file=sys.stderr)
        return
    committed = retry(_git, "commit", "-qm", message)
    combined = (committed.stdout or "") + (committed.stderr or "")
    # nothing-to-commit 是 git 的正常无操作返回，不算失败
    if committed.returncode != 0 and "nothing to commit" not in combined:
        print(f"compound-memory: git commit failed: {combined.strip()}", file=sys.stderr)


def recover_orphan_changes(_git: GitRunner, lock: LockProvider, do_commit: Callable[[str], None]) -> None:
    """启动对账：上次会话 commit 失败/进程中断留在工作树的孤儿变更，
    收编进一个明确标注的恢复提交——否则它们会被下一个写动词的
    "write ..." 消息错位归因（2026-10-02 实测）。带外手编未提交的
    变更同样会被收编：恢复消息不声称作者，语义上诚实；需要专属提交
    历史的带外变更应在手编流程内自行 commit。
    """
    with lock():  # status 判定与收编同临界区：锁外判定的 TOCTOU 窗口会漏变更
        status = _git("status", "--porcelain", check=False)
        if status.returncode != 0 or not status.stdout.strip():
            return  # 坏仓库/干净树零副作用：启动路径宁降级
        do_commit("orphan changes recovered")


def log_lines(_git: GitRunner, limit: int, enabled: bool) -> list[str]:
    """审计史读取（git_log 的实现体）：响亮读路径，check=True。"""
    if not enabled:
        return []
    proc = _git("log", "--oneline", f"-{limit}", check=True)
    return [line for line in proc.stdout.splitlines() if line.strip()]
