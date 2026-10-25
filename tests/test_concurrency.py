"""多写入者并发协调（#21）：root 级 advisory lock。

flock 关联 open file description——同进程两个 store 各自 open() 也互斥
（PoC 实证），故跨进程互斥可用线程级并发复现；git 是外部子进程，
index.lock 竞争真实存在于跨线程场景（2026-10-02 实测：并发 feedback
撞锁报错 + git add -A 扫进他人变更）。
"""

from __future__ import annotations

import io
import logging
import os
import subprocess
import threading
import time
from contextlib import redirect_stderr
from multiprocessing import Process, Queue
from pathlib import Path

import pytest

from compound_memory.storage import MemoryStore
from compound_memory.storage.files import _unlink_file
from compound_memory.storage.locking import WriteLockError, _spin_until_locked

from conftest import CLOCK_DATE

WRITES_PER_WRITER = 8


def _make_store(root: Path) -> MemoryStore:
    """每个写入者独立 store 实例：独立索引缓存，模拟独立进程。"""
    return MemoryStore(root, clock=lambda: CLOCK_DATE, remover=_unlink_file)


class TestConcurrentWriters:
    def test_parallel_writes_no_loss_no_git_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        root = tmp_path / "memroot"
        _make_store(root).write("seed", type="fact", source="agent-a")

        # 放大 index.lock 撞车窗口：无锁实现下本测试必然暴露竞态
        orig_run = subprocess.run

        def slow_run(*args: object, **kwargs: object):
            time.sleep(0.02)
            return orig_run(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(subprocess, "run", slow_run)

        ids: list[str] = []
        errors: list[str] = []

        def writer(name: str) -> None:
            buf = io.StringIO()
            try:
                with redirect_stderr(buf):
                    store = _make_store(root)
                    for i in range(WRITES_PER_WRITER):
                        result = store.write(f"{name}-{i}", type="fact", source=name)
                        ids.append(result["id"])
            except Exception as exc:  # noqa: BLE001 - 并发故障形态收集进断言
                import traceback

                errors.append(f"{name}: {exc!r}\n{traceback.format_exc()}")
            if "git add failed" in buf.getvalue() or "git commit failed" in buf.getvalue():
                errors.append(f"{name}: git failure surfaced on stderr:\n{buf.getvalue()}")

        threads = [threading.Thread(target=writer, args=(f"agent-{n}",)) for n in "bc"]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads), "writer thread hung: deadlock?"
        assert errors == []
        # 无写丢失：两写入者的每一条都落盘可查（按 id 读，不受 top_k 截断影响）
        store = _make_store(root)
        assert len(ids) == 2 * WRITES_PER_WRITER
        assert all(store.find(mid) is not None for mid in ids)

    def test_lock_unavailable_warns_and_proceeds(self, store: MemoryStore, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
        """锁不可用的异常环境：响亮告警 + 降级无锁执行，不静默、不死锁。"""
        real_open = os.open

        def deny_lock_file(path: object, *args: object, **kwargs: object) -> int:
            if str(path).endswith(".lock"):
                raise OSError("lock fs unavailable")
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(os, "open", deny_lock_file)
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            result = store.write("still lands", type="fact", source="agent-a")
        assert store.find(result["id"]) is not None
        msgs = [r.getMessage() for r in caplog.records]
        assert any("write lock unavailable" in m for m in msgs)

    def test_lock_acquire_failure_refuses_write(self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch) -> None:
        """争锁失败 / 超时必须 fail-closed（#58）：不落盘、不走无锁写入。"""
        def deny(fd: int) -> None:
            raise OSError("timed out after 30s waiting for the write lock")

        monkeypatch.setattr("compound_memory.storage.locking._lock_exclusive", deny)
        before = list(store.ns_root.rglob("*.md"))
        with pytest.raises(WriteLockError, match="refusing unlocked write"):
            store.write("must not land", type="fact", source="agent-a")
        assert list(store.ns_root.rglob("*.md")) == before

    def test_lock_wait_timeout_raises_without_sleeping(self) -> None:
        """Windows 轮询上界：注入时钟，超时即 OSError，不依赖真实 30 秒。"""
        clock = {"t": 0.0}

        def lock() -> None:
            raise OSError("busy")

        def monotonic() -> float:
            return clock["t"]

        def sleep(dt: float) -> None:
            clock["t"] += dt

        with pytest.raises(OSError, match="timed out after 0s"):
            _spin_until_locked(lock, sleep=sleep, monotonic=monotonic, timeout=0.0, poll=0.05)
        assert clock["t"] == 0.0

        clock["t"] = 0.0
        calls = {"n": 0}

        def lock_then_ok() -> None:
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("busy")

        _spin_until_locked(lock_then_ok, sleep=sleep, monotonic=monotonic, timeout=1.0, poll=0.05)
        assert calls["n"] == 3

    def test_batch_holds_lock_reentrancy_no_deadlock(self, tmp_path: Path) -> None:
        """batch 全程持锁：批内动词重入不死锁，批尾 flush+commit 同在临界区。"""
        store = _make_store(tmp_path / "memroot")
        with store.batch("bulk write") as batch_ctx:
            for i in range(3):
                store.write(f"batched {i}", type="fact", source="agent-a")
            batch_ctx.message = "bulk write test"
        assert store.stats()["active"] == 3

    def test_parallel_feedback_no_lost_update(self, tmp_path: Path) -> None:
        """并发 feedback 同一记忆：读-改-写必须全程在临界区内。

        find 在锁外时两写者读到同一快照、后写覆盖前者，uses/confidence 丢更新
        （2026-10-02 实测的「重试重复 +uses」同根因）。
        """
        root = tmp_path / "memroot"
        mem_id = _make_store(root).write("hot memory", type="fact", source="agent-a")["id"]
        rounds = 8
        barrier = threading.Barrier(2)

        def bump(agent: str) -> None:
            store = _make_store(root)
            barrier.wait()
            for _ in range(rounds):
                store.feedback(mem_id, agent=agent)

        threads = [threading.Thread(target=bump, args=(f"agent-{n}",)) for n in "bc"]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads), "feedback thread hung: deadlock?"
        mem = _make_store(root).find(mem_id)
        assert mem is not None
        assert mem.uses == 2 * rounds, f"lost updates under concurrent feedback: uses={mem.uses}"

    def test_same_store_parallel_feedback_no_lost_update(self, tmp_path: Path) -> None:
        """同一 MemoryStore 上的双线程 feedback：重入深度不得当成跨线程直通。

        上面的用例给每个线程新建 store，flock 仍能串行化。MCP 持有的是单例
        store：实例级 lock_depth>0 时另一线程会走重入快路径、跳过 flock，
        两轮 feedback 读到同一快照并互相覆盖（#107）。

        start barrier 让两线程一起进入循环。第一次落盘再等一次 in-section
        barrier：误重入时对端也在临界区内，两边一起放行、写出同一快照；
        锁正确时对端进不了 _save，本线程超时后单独继续。最终 uses 必须等于
        两边轮数之和。纯靠调度去撞 lock_depth 窗口会漏报（临界区一短就串行成功）。
        """
        root = tmp_path / "memroot"
        store = _make_store(root)
        mem_id = store.write("hot memory", type="fact", source="agent-a")["id"]
        rounds = 8
        start = threading.Barrier(2)
        in_section = threading.Barrier(2)
        errors: list[str] = []
        real_save = store._save
        state = {"n": 0}
        state_lock = threading.Lock()

        def overlapped_save(mem: object) -> None:
            participate = False
            with state_lock:
                if state["n"] < 2:
                    state["n"] += 1
                    participate = True
            if participate:
                try:
                    # 对端若能在本线程仍持锁时进入落盘，会立刻凑齐；否则超时。
                    in_section.wait(timeout=2.0)
                except threading.BrokenBarrierError:
                    pass
            real_save(mem)  # type: ignore[arg-type]

        store._save = overlapped_save  # type: ignore[method-assign]

        def bump(agent: str) -> None:
            buf = io.StringIO()
            try:
                start.wait()
                with redirect_stderr(buf):
                    for _ in range(rounds):
                        store.feedback(mem_id, agent=agent)
            except Exception as exc:  # noqa: BLE001 - 并发故障形态收集进断言
                import traceback

                errors.append(f"{agent}: {exc!r}\n{traceback.format_exc()}")
            stderr = buf.getvalue()
            if "git add failed" in stderr or "git commit failed" in stderr:
                errors.append(f"{agent}: git failure surfaced on stderr:\n{stderr}")

        threads = [threading.Thread(target=bump, args=(f"agent-{n}",)) for n in "bc"]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert not any(t.is_alive() for t in threads), "feedback thread hung: deadlock?"
        assert errors == []
        mem = store.find(mem_id)
        assert mem is not None
        assert mem.uses == 2 * rounds, f"lost updates under same-store concurrent feedback: uses={mem.uses}"

    def test_batch_reentry_stays_on_held_lock(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同线程 batch 重入仍直通已持有的锁，不再 open .lock，也不降级成无锁写。

        重入若退化成第二次加锁，POSIX 上另一 fd 的 flock 会自锁（本测试挂死）；
        若退化成锁文件失败的无锁路径，会打出 write lock unavailable。
        """
        store = _make_store(tmp_path / "memroot")
        real_open = os.open
        opens = {"n": 0}

        def counting_open(path: object, *args: object, **kwargs: object) -> int:
            if str(path).endswith(".lock"):
                opens["n"] += 1
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        depths: list[int] = []
        real_commit = store._commit

        def probing_commit(message: str) -> None:
            depths.append(store._write_lock_depth)
            real_commit(message)

        store._commit = probing_commit  # type: ignore[method-assign]
        monkeypatch.setattr(os, "open", counting_open)
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            with store.batch("bulk write") as batch_ctx:
                for i in range(3):
                    store.write(f"batched {i}", type="fact", source="agent-a")
                batch_ctx.message = "bulk write test"
        assert store.stats()["active"] == 3
        # 外层 batch 打开一次 .lock；批内三次 write 重入，不再打开。
        assert opens["n"] == 1, f"inner writes re-opened the lock file: opens={opens['n']}"
        assert any(d > 1 for d in depths), f"inner write did not reenter: depths={depths}"
        assert all(d > 0 for d in depths)
        assert not any("write lock unavailable" in r.getMessage() for r in caplog.records)


def _mp_worker(root_str: str, name: str, n: int, q: "Queue[tuple]") -> None:
    """真跨进程写入者（#21 验收）：独立进程、独立 store，写 n 条并回报。"""
    try:
        buf = io.StringIO()
        with redirect_stderr(buf):
            store = MemoryStore(Path(root_str), clock=lambda: CLOCK_DATE, remover=_unlink_file)
            ids = [store.write(f"{name}-{i}", type="fact", source=name)["id"] for i in range(n)]
        git_failed = "git add failed" in buf.getvalue() or "git commit failed" in buf.getvalue()
        q.put(("ok", ids, git_failed, ""))
    except Exception as exc:  # noqa: BLE001 - 故障形态完整回报给断言侧
        q.put(("err", [], False, repr(exc)))


class TestCrossProcessWriters:
    """真多进程验收（#21 brief 字面要求）：flock 的跨进程互斥语义。"""

    def test_two_processes_write_no_loss_no_git_failure(self, tmp_path: Path) -> None:
        root = tmp_path / "memroot"
        _make_store(root).write("seed", type="fact", source="agent-a")
        q: "Queue[tuple]" = Queue()
        procs = [Process(target=_mp_worker, args=(str(root), f"agent-{n}", 5, q)) for n in "bc"]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
        assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]
        results = [q.get(timeout=10) for _ in procs]
        assert all(status == "ok" for status, *_ in results), results
        assert not any(git_failed for _, _, git_failed, _ in results), results
        ids = [mid for _, ids, _, _ in results for mid in ids]
        assert len(ids) == 10
        store = _make_store(root)
        assert all(store.find(mid) is not None for mid in ids)
