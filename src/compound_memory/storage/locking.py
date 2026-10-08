"""写锁与批式落库协调（机制件，#36 片 e）：跨进程互斥、重入、批式收尾。

单一定义点：写锁语义（跨进程互斥/重入/降级告警）与 batch 协调（嵌套拒绝、
批内 commit 延迟计数、批尾 flush+commit 收尾）都在本模块；状态归
WriteLocker 对象，facade 持有并委托。

平台语义（#55）：POSIX 走 flock，Windows 走 msvcrt 字节范围锁（1 字节 @
偏移 0）。等待语义有平台差异：flock 阻塞直至获得锁；LK_LOCK 被占时每秒
重试、约 10 秒仍失败抛 OSError——与锁机制不可用同路，落入既有「降级无锁 +
warning」路径（宁降级勿死锁，哲学两平台一致，仅等待上界为平台差异）。
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Protocol

logger = logging.getLogger(__name__)


def _lock_exclusive(fd: int) -> None:
    """fd 上的跨进程独占锁：POSIX 走 flock，Windows 走 msvcrt 字节范围锁（#55）。

    msvcrt.locking 锁「当前位置起的 n 字节」，故先 seek 到 0；锁定范围与解锁
    必须同 fd 同偏移同长度配对（见 _unlock_fd）。
    """
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        # LK_LOCK：被占时每秒重试，约 10 秒仍失败抛 OSError → 调用方降级无锁
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX)


def _unlock_fd(fd: int) -> None:
    """与 _lock_exclusive 配对的解锁（同 fd、同偏移、同长度）。"""
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


class _Batch:
    """batch() 的句柄：允许批内覆写提交消息（distill_apply 的溯源消息在产物写入后才凑得齐 id）。"""

    def __init__(self, message: str | None = None) -> None:
        self.message = message


class _DeferTarget(Protocol):
    """batch 收尾要触达的两份缓存（词法/向量）的最小面。"""

    def defer(self) -> None: ...

    def flush_pending(self) -> None: ...


class WriteLocker:
    """写锁 + batch 协调状态单点（挂在 root 的 .lock 上跨进程互斥，机制见模块 docstring 平台语义）。

    深度状态：lock_depth 写锁重入（batch 持锁期间批内动词直通）、
    batch_depth 嵌套深度（恒 0 或 1：嵌套 batch 是调用方错误）、
    batch_ops 本批延迟的提交计数（批尾消息与「零操作不提交」判据）。
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self.lock_depth = 0  # 写锁重入深度：batch 持锁期间批内动词直通
        self.batch_depth = 0  # batch() 嵌套深度（恒 0 或 1：嵌套 batch 是调用方错误）
        self.batch_ops = 0  # 本批延迟的提交计数（批尾消息与"零操作不提交"判据）

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """写路径动词的跨进程互斥（#21）：覆盖「文件写出 + 缓存更新 + commit」临界区。

        多宿主并发写同一 root 时，git add -A 会扫进他人刚落盘的变更、commit 撞
        index.lock 报错（2026-10-02 实测）；flock 串行化写者后两者皆消。flock 关联
        open file description，同进程重复加锁会自锁——batch 持锁期间批内动词经
        depth 重入直通。锁不可用的异常环境降级无锁并 warning（宁降级勿死锁）；
        读路径与检索不持锁（索引缓存自身并发安全，见 Index._save 的唯一临时名）。
        """
        if self.lock_depth > 0:
            self.lock_depth += 1
            try:
                yield
            finally:
                self.lock_depth -= 1
            return
        fd: int | None = None
        try:
            fd = os.open(self._root / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
            _lock_exclusive(fd)
        except OSError as exc:
            logger.warning("write lock unavailable: %s; proceeding unlocked", exc)
            if fd is not None:
                os.close(fd)
                fd = None
        self.lock_depth += 1
        try:
            yield
        finally:
            self.lock_depth -= 1
            if fd is not None:
                try:
                    _unlock_fd(fd)
                finally:
                    os.close(fd)

    def defer_commit(self) -> bool:
        # 批内延迟：commit 收拢到 batch() 退出时一次性执行（单点拦截，各动词无需批式特化）
        if self.batch_depth > 0:
            self.batch_ops += 1
            return True
        return False

    @contextmanager
    def batch(
        self,
        message: str | None,
        index: _DeferTarget,
        vector_index: _DeferTarget,
        do_commit: Callable[[str], None],
    ) -> Iterator[_Batch]:
        """批量落库的协调体（facade.batch 的实现，行为契约见其 docstring）。

        - 每条 write 照常逐条校验并立即落盘（写穿），变化只在提交粒度；
        - 失败语义「落地即已提交」：批内异常时已写入条目照常 flush + commit
          （消息注明 partial）后原样上抛——不存在静默半提交；
        - 嵌套 batch 是调用方错误（ValueError）。
        """
        if self.batch_depth > 0:
            raise ValueError("nested batch() is not supported")
        with self.write_lock():  # 全程持锁：批内写穿与批尾 flush+commit 同在临界区
            self.batch_depth += 1
            self.batch_ops = 0
            handle = _Batch(message)
            index.defer()
            vector_index.defer()
            try:
                yield handle
            except BaseException:
                self._end_batch(handle.message, partial=True, index=index, vector_index=vector_index, do_commit=do_commit)
                raise
            self._end_batch(handle.message, partial=False, index=index, vector_index=vector_index, do_commit=do_commit)

    def _end_batch(
        self,
        message: str | None,
        partial: bool,
        index: _DeferTarget,
        vector_index: _DeferTarget,
        do_commit: Callable[[str], None],
    ) -> None:
        # 先退出批态再 flush：flush 与收尾 commit 不被延迟拦截
        self.batch_depth -= 1
        index.flush_pending()
        vector_index.flush_pending()
        if self.batch_ops:
            suffix = " (partial)" if partial else ""
            do_commit((message + suffix) if message else f"batch write {self.batch_ops} entries{suffix}")
            self.batch_ops = 0
