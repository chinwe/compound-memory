"""storage 健壮性：原子写出（#18）/ 扫描容错（#20）/ 降级可观测（#19）。

storage 属敏感区，遵循仓库 TDD 约定：用例先于实现（先红后绿）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import compound_memory.storage as storage_mod
from compound_memory.storage import MemoryStore, _unlink_file

from conftest import CLOCK_DATE

BAD_FILE_CORPUS = {
    "no-frontmatter": "not frontmatter at all\n",
    "broken-yaml": "---\nkey: [unclosed\n---\nbody\n",
    "bad-encoding": None,  # 以非法 utf-8 字节写入
    # 合法 YAML 但非映射（手编常见坏法）：同样必须被容错面捕获
    "scalar-yaml": "---\njust a bare string\n---\nbody\n",
    "list-yaml": "---\n- one\n- two\n---\nbody\n",
}


def _make_store(tmp_path: Path) -> MemoryStore:
    """本文件的失败注入场景会连带炸掉 os.replace 改名型的沙箱 remover，
    故注入真 unlink adapter——单文件 unlink 不受沙箱批量删除守卫影响。"""
    return MemoryStore(tmp_path / "memroot", clock=lambda: CLOCK_DATE, remover=_unlink_file)


class TestAtomicSave:
    """#18：_save 是全部写动词共用的单文件写出点。

    中断/失败时目标文件要么保持旧完整内容、要么变成新完整内容，
    绝不留半写文件（坏 frontmatter 会与全量扫描联动放大故障面）；
    任何路径都不得残留临时文件。
    """

    def test_successful_writes_leave_only_md_files(self, tmp_path: Path) -> None:
        store = _make_store(tmp_path)
        store.write("alpha fact", type="fact", source="agent-a", key="k1")
        store.write("beta episode", type="episode", source="agent-a")
        stray = [
            p
            for base in (store.ns_root, store.archive_root)
            for p in base.rglob("*")
            if p.is_file() and p.suffix != ".md"
        ]
        assert stray == []

    def test_failed_replace_keeps_old_content(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _make_store(tmp_path)
        mem_id = store.write("original", type="fact", source="agent-a")["id"]
        store.feedback(mem_id, agent="agent-b")  # 正常一轮反馈：uses=1 写回同一文件

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated interruption before replace")

        monkeypatch.setattr(storage_mod.os, "replace", boom)
        with pytest.raises(OSError):
            store.feedback(mem_id, agent="agent-b")
        monkeypatch.undo()

        # 旧内容原封不动：替换前的任何失败都不许碰目标文件
        mem = store.find(mem_id)
        assert mem is not None
        assert mem.uses == 1
        # 失败路径同样清理临时文件，目录里只剩合法 .md
        stray = [p for p in (store.ns_root / "_shared" / "fact").iterdir() if p.suffix != ".md"]
        assert stray == []

    def test_written_file_permissions_match_plain_write(self, tmp_path: Path) -> None:
        """#18 验收「与现状一致」含权限：mkstemp 固定 0600 会整体变严，
        必须对齐 open() 默认（0666 & ~umask）。"""
        import os

        store = _make_store(tmp_path)
        mem_id = store.write("perm check", type="fact", source="agent-a")["id"]
        path = next((store.ns_root / "_shared" / "fact").rglob(f"{mem_id}.md"))
        mask = os.umask(0)
        os.umask(mask)
        expected = 0o666 & ~mask
        assert path.stat().st_mode & 0o777 == expected


class TestReviewQueueAtomicResolve:
    """清行落盘与记忆文件同规格（spec：文件写出一律 atomic_write_text）：
    resolve 是 review-queue.md 的唯一改写点，中断时旧队列原封保留——
    解析侧「宁可不登记，不误删记录」的 fail-safe 延伸到写侧，
    半写队列文件与坏 frontmatter 一样会放大解析失败面。"""

    def test_failed_replace_keeps_queue_intact(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        store = _make_store(tmp_path)
        store.write("original content", type="fact", source="agent-d", key="k1")
        store.write("conflicting content", type="fact", source="agent-d", key="k1")
        assert len(store.review_queue()) == 1

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("simulated interruption before replace")

        monkeypatch.setattr(storage_mod.os, "replace", boom)
        with pytest.raises(OSError):
            store.review_resolve(all=True)
        monkeypatch.undo()

        # 裁决登记的改写要么整体生效要么不生效：中断不清队
        assert len(store.review_queue()) == 1
        # 失败路径清理临时文件，根目录只剩既有 artifact
        stray = [
            p
            for p in store.root.iterdir()
            if p.is_file() and p.suffix != ".md" and p.name not in {".gitignore", ".lock"}
        ]
        assert stray == []


def _plant_bad_file(store: MemoryStore, kind: str = "no-frontmatter") -> Path:
    """带外制造坏记忆文件（模拟手编/写入中断产物），返回其路径。"""
    bad = store.ns_root / "_shared" / "fact" / "19990101_badfile.md"
    if BAD_FILE_CORPUS[kind] is None:
        bad.write_bytes(b"\xff\xfe\x00not utf-8")
    else:
        bad.write_text(BAD_FILE_CORPUS[kind], encoding="utf-8")
    return bad


class TestScanTolerance:
    """#20：单个坏文件不得炸掉任何扫描路径。

    缺 frontmatter / 坏 YAML / 编码错的文件被跳过并 warning 告警
    （响亮但不阻断），文件原样保留待人工处置；全好文件时零额外日志。
    """

    @pytest.mark.parametrize(
        "kind", ["no-frontmatter", "broken-yaml", "bad-encoding", "scalar-yaml", "list-yaml"]
    )
    def test_rebuild_skips_bad_file(self, store: MemoryStore, caplog: pytest.LogCaptureFixture, kind: str) -> None:
        store.write("good fact", type="fact", source="agent-a")
        bad = _plant_bad_file(store, kind)
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            counts = store.rebuild_index()
        assert counts["memories"] == 1  # 计数不含坏文件
        assert any("19990101_badfile" in r.getMessage() for r in caplog.records)
        # #20 验收：除逐条告警外还有数量汇总
        msgs = [r.getMessage() for r in caplog.records]
        assert any("skipped" in m and "unparseable" in m for m in msgs)
        assert bad.exists()  # 不删除、不移动

    def test_stats_skips_bad_file(self, store: MemoryStore, caplog: pytest.LogCaptureFixture) -> None:
        store.write("good fact", type="fact", source="agent-a")
        _plant_bad_file(store)
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            out = store.stats()
        assert out["total"] == 1
        assert any("19990101_badfile" in r.getMessage() for r in caplog.records)

    def test_distill_plan_skips_bad_file(self, store: MemoryStore, caplog: pytest.LogCaptureFixture) -> None:
        store.write("good episode", type="episode", source="agent-a")
        _plant_bad_file(store)
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            out = store.distill_plan(min_uses=0)  # 新写入记忆 uses=0，关掉活性门槛只验容错
        assert [c["content"] for c in out["candidates"]] == ["good episode"]

    def test_decay_sweep_skips_bad_file(self, store: MemoryStore, caplog: pytest.LogCaptureFixture) -> None:
        store.write("good fact", type="fact", source="agent-a")
        bad = _plant_bad_file(store)
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            swept = store.decay_sweep()
        assert isinstance(swept, list)
        assert any("19990101_badfile" in r.getMessage() for r in caplog.records)
        assert bad.exists()

    def test_write_key_conflict_scan_skips_bad_file(self, store: MemoryStore, caplog: pytest.LogCaptureFixture) -> None:
        """write 的 key 冲突检测也扫目录：坏文件不得阻断新写入。"""
        store.write("first fact", type="fact", source="agent-a", key="k1")
        _plant_bad_file(store)
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            result = store.write("second fact", type="fact", source="agent-a", key="k2")
        assert result["conflict"] is False

    def test_clean_scan_has_no_warning(self, store: MemoryStore, caplog: pytest.LogCaptureFixture) -> None:
        store.write("good fact", type="fact", source="agent-a")
        store.write("good episode", type="episode", source="agent-a")
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            store.rebuild_index()
            store.stats()
        assert caplog.records == []


class TestVectorDegradeVisibility:
    """#19：向量召回的真实故障必须可观测（warning 日志，宁缺勿炸语义不变）；
    未注入 embedder 是正常配置路径，不产生日志——让用户能区分
    「没装 vec extra」与「索引/模型真坏了」。"""

    def test_embedder_fault_degrades_loudly(
        self, vec_store: MemoryStore, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vec_store.write("semantic hit", type="fact", source="agent-a")

        def broken(texts: list[str]) -> list[list[float]]:
            raise RuntimeError("model exploded")

        # 只在检索侧注入故障：写路径的编码失败是响亮抛错（另策），#19 管的是
        # 检索降级的静默
        monkeypatch.setattr(vec_store, "_embedder", broken)
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            hits = vec_store.search("semantic")
        # 降级纯词面仍命中：告警不改变检索行为
        assert [h["content"] for h in hits] == ["semantic hit"]
        msgs = [r.getMessage() for r in caplog.records]
        assert any("vector" in m.lower() and "model exploded" in m for m in msgs)

    def test_missing_embedder_is_silent(self, store: MemoryStore, caplog: pytest.LogCaptureFixture) -> None:
        store.write("plain fact", type="fact", source="agent-a")
        with caplog.at_level(logging.WARNING, logger="compound_memory.storage"):
            store.search("plain")
        assert caplog.records == []
