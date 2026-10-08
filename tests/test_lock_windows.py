"""Windows 专属锁路径（#55）：msvcrt 分支在非 Windows 平台整文件跳过，
由 CI 的 windows-latest 矩阵真实执行——macOS 本地跑不到，不构成已验证。

锁的平台语义（等待上界、降级哲学）单点在 locking 模块 docstring，此处不
复述；本文件只验证跨平台等价的那部分契约：跨进程争锁必须在对方释放后才
获准，且是在 LK_LOCK 重试窗内等到、而非骑上 10 秒降级路径混进临界区。
LK_LOCK 超窗走降级的路径两平台共用，已由 test_concurrency 的降级用例覆盖。
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from compound_memory.storage.locking import WriteLocker

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="msvcrt 专属路径，仅 Windows 执行")


def test_lock_roundtrip(tmp_path: Path) -> None:
    """同进程加锁/解锁一个来回：msvcrt 路径（seek + 1 字节范围锁）不抛错、深度归位。"""
    locker = WriteLocker(tmp_path)
    with locker.write_lock():
        assert locker.lock_depth == 1
    assert locker.lock_depth == 0


def test_second_process_waits_for_release(tmp_path: Path) -> None:
    """跨进程争锁：持锁子进程 2 秒后才释放，第二个进程必须等到释放后才获锁。

    握手用哨兵文件而非管道 readline（readline 无超时，子进程启动失败会挂死
    CI）：子进程成功加锁后落哨兵，主进程带截止时间轮询哨兵，子进程没锁上则
    响亮失败而非抢跑。waited 下界证明确实等待过；上界 9 秒排除「LK_LOCK
    超窗降级无锁混入」——那不是互斥，是降级。
    """
    lock_path, sentinel = tmp_path / ".lock", tmp_path / "holder.locked"
    holder = (
        "import os, sys, time, msvcrt\n"
        "fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR)\n"
        "os.lseek(fd, 0, os.SEEK_SET)\n"
        "msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)\n"
        "open(sys.argv[2], 'w').close()\n"
        "time.sleep(2)\n"
        "os.lseek(fd, 0, os.SEEK_SET)\n"
        "msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)\n"
        "os.close(fd)\n"
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
        locker = WriteLocker(tmp_path)
        start = time.monotonic()
        with locker.write_lock():
            waited = time.monotonic() - start
    finally:
        proc.wait(timeout=15)
    # 功能性互斥断言（非性能钉）：哨兵后持锁方还睡 2 秒，下界 1 秒、
    # 上界 9 秒（LK_LOCK 窗约 10 秒，留 1 秒余量）
    assert 1.0 <= waited < 9.0, f"exclusion broken or degraded: acquired after {waited:.2f}s"
