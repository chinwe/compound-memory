"""多写入者并发协调（#21）：root 级 advisory lock。

flock 关联 open file description——同进程两个 store 各自 open() 也互斥
（PoC 实证），故跨进程互斥可用线程级并发复现；git 是外部子进程，
index.lock 竞争真实存在于跨线程场景（2026-10-02 实测：并发 feedback
撞锁报错 + git add -A 扫进他人变更）。
"""

from __future__ import annotations

import io
import logging
import threading
import time
from contextlib import redirect_stderr
from pathlib import Path

import pytest

import compound_memory.storage as storage_mod
from compound_memory.storage import MemoryStore, _unlink_file

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
        orig_run = storage_mod.subprocess.run

        def slow_run(*args: object, **kwargs: object):
            time.sleep(0.02)
            return orig_run(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(storage_mod.subprocess, "run", slow_run)

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
        real_open = storage_mod.os.open

        def deny_lock_file(path: object, *args: object, **kwargs: object) -> int:
            if str(path).endswith(".lock"):
                raise OSError("lock fs unavailable")
            return real_open(path, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(storage_mod.os, "open", deny_lock_file)
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            result = store.write("still lands", type="fact", source="agent-a")
        assert store.find(result["id"]) is not None
        msgs = [r.getMessage() for r in caplog.records]
        assert any("write lock unavailable" in m for m in msgs)

    def test_batch_holds_lock_reentrancy_no_deadlock(self, tmp_path: Path) -> None:
        """batch 全程持锁：批内动词重入不死锁，批尾 flush+commit 同在临界区。"""
        store = _make_store(tmp_path / "memroot")
        with store.batch("bulk write") as batch_ctx:
            for i in range(3):
                store.write(f"batched {i}", type="fact", source="agent-a")
            batch_ctx.message = "bulk write test"
        assert store.stats()["active"] == 3
