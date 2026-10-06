"""VecEngine 引擎缝测试：SqliteVecEngine 直测 + fake engine 钉编排不变量。

引擎缝（ADR 0004 / issue #40）：vector_engine 只存不算——content-hash 计算
与 embedder 调用留编排层；vector_index 降纯编排。两层各钉各的：
- SqliteVecEngine 直测：存取往返、knn 的 L2→余弦换算、rebuild 整体替换、
  跨实例持久（协议签名与 entries() 形状的契约面）；
- FakeEngine 编排测：带外对账只编码 diff、引擎不可用全程 no-op 降级、
  batch 批内暂存批尾一次落库——这些不变量属编排层，换引擎不得漂移。
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable

import pytest

from compound_memory.model import Memory
from compound_memory.vector_engine import SqliteVecEngine, VectorEngineError
from compound_memory.vector_index import VectorIndex


def make_engine(tmp_path: Path, dim: int = 4) -> SqliteVecEngine:
    return SqliteVecEngine(tmp_path / "vectors.db", dim=dim)


# ---------- 编排层测试件 ----------


def make_mem(mid: str, content: str, archived: bool = False) -> Memory:
    return Memory(
        id=mid, ns="_shared", type="fact", source="agent-a",
        created="2026-01-01", content=content, archived=archived,
    )


class CountingEmbedder:
    """包装 embedder 统计调用次数——验证编排层的零重复编码不变量。"""

    def __init__(self, inner: Callable[[list[str]], list[list[float]]]) -> None:
        self.inner = inner
        self.calls = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return self.inner(texts)


def unit_embedder() -> Callable[[list[str]], list[list[float]]]:
    """确定性 embedder：全部输出同一单位向量（测试不依赖向量内容）。"""

    def embed(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    return embed


def vec_db_path(root: Path) -> Path:
    """编排层 mtime 基线盯的 db 路径——fake 引擎必须在此落实体文件。"""
    return root / "index" / "vectors.db"


def _shift_mtime(path: Path, seconds: float) -> None:
    """目录 mtime 相对墙钟平移——绕开文件系统时间戳粒度，stale 判定确定。"""
    when = time.time() + seconds
    os.utime(path, (when, when))


class FakeEngine:
    """内存版 VecEngine：调用全记录，open 结果与故障点可注入。

    在 vec_db_path 落一个标记文件承载编排层的 mtime 基线（真实引擎的
    db 文件同位）——编排层的活性/对账逻辑因此按生产行为运转。
    """

    def __init__(self, path: Path, *, open_ok: bool = True, fail_on: str | None = None) -> None:
        self._path = path
        self._open_ok = open_ok
        self.fail_on = fail_on  # 测试中途可改（注入/解除故障）
        self.meta: dict[str, tuple[str, str]] = {}  # mem_id -> (content_hash, rel_path)
        self.vecs: dict[str, list[float]] = {}
        self.upserts: list[str] = []
        self.removes: list[str] = []
        self.rel_updates: list[tuple[str, str]] = []
        self.commits = 0
        self.rebuilds: list[list[str]] = []  # 每次 rebuild 的 mem_id 列表

    def _fail(self, op: str) -> None:
        if self.fail_on == op:
            raise VectorEngineError(f"injected failure on {op}")

    def _touch_db(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("fake", encoding="utf-8")

    def open(self) -> bool:
        if not self._open_ok:
            return False
        self._touch_db()
        return True

    def close(self) -> None:
        pass  # 内存引擎无句柄

    def upsert(self, mem_id: str, rel_path: str, ns: str, content_hash: str, vec: list[float]) -> None:
        self._fail("upsert")
        self.upserts.append(mem_id)
        self.meta[mem_id] = (content_hash, rel_path)
        self.vecs[mem_id] = vec

    def remove(self, mem_id: str) -> None:
        self._fail("remove")
        self.removes.append(mem_id)
        self.meta.pop(mem_id, None)
        self.vecs.pop(mem_id, None)

    def update_rel(self, mem_id: str, rel_path: str) -> None:
        self._fail("update_rel")
        self.rel_updates.append((mem_id, rel_path))
        if mem_id in self.meta:
            self.meta[mem_id] = (self.meta[mem_id][0], rel_path)

    def entries(self) -> dict[str, tuple[str, str]]:
        self._fail("entries")
        return dict(self.meta)

    def knn(self, query_vec: list[float], k: int) -> list[tuple[str, str, float]]:
        self._fail("knn")
        scored = sorted(
            ((sum(a * b for a, b in zip(v, query_vec)), mid) for mid, v in self.vecs.items()),
            reverse=True,
        )
        return [(mid, self.meta[mid][1], cos) for cos, mid in scored[:k]]

    def rebuild(self, rows: list[tuple[str, str, str, str, list[float]]]) -> None:
        self._fail("rebuild")
        self.rebuilds.append([r[0] for r in rows])
        self.meta = {r[0]: (r[3], r[1]) for r in rows}
        self.vecs = {r[0]: r[4] for r in rows}
        self._touch_db()

    def commit(self) -> None:
        self._fail("commit")
        self.commits += 1
        self._touch_db()


def make_vi(
    root: Path,
    mems: dict[str, tuple[Memory, str]],
    engine: FakeEngine,
    embedder: Callable[[list[str]], list[list[float]]],
) -> VectorIndex:
    return VectorIndex(
        root, scan_pairs=lambda: list(mems.values()), embedder=embedder, engine=engine
    )


class TestSqliteVecEngineContract:
    def test_open_upsert_entries_roundtrip(self, tmp_path: Path):
        """open 建连建表；upsert 后 entries 快照给出 (content_hash, rel_path)。"""
        engine = make_engine(tmp_path)
        assert engine.open() is True
        engine.upsert("m1", "_shared/fact/m1.md", "_shared", "hash1", [1.0, 0.0, 0.0, 0.0])
        engine.commit()
        assert engine.entries() == {"m1": ("hash1", "_shared/fact/m1.md")}

    def test_upsert_replaces_row_wholesale(self, tmp_path: Path):
        """同 id 再 upsert 整体替换：新 hash 生效、旧向量不留孤儿（knn 只回新向量）。"""
        engine = make_engine(tmp_path)
        engine.open()
        engine.upsert("m1", "r1", "_shared", "h1", [1.0, 0.0, 0.0, 0.0])
        engine.upsert("m1", "r2", "_shared", "h2", [0.0, 1.0, 0.0, 0.0])
        engine.commit()
        assert engine.entries() == {"m1": ("h2", "r2")}
        hits = engine.knn([0.0, 1.0, 0.0, 0.0], 5)
        assert [h[0] for h in hits] == ["m1"]
        assert hits[0][2] == pytest.approx(1.0)  # 命中的是新向量，非旧向量残留

    def test_update_rel_moves_path_only(self, tmp_path: Path):
        """update_rel 只改 rel_path，不动 hash/向量（手编挪位的对账路径）。"""
        engine = make_engine(tmp_path)
        engine.open()
        engine.upsert("m1", "r1", "_shared", "h1", [1.0, 0.0, 0.0, 0.0])
        engine.update_rel("m1", "r2")
        engine.commit()
        assert engine.entries() == {"m1": ("h1", "r2")}

    def test_remove_drops_entry_and_is_noop_when_absent(self, tmp_path: Path):
        engine = make_engine(tmp_path)
        engine.open()
        engine.upsert("m1", "r1", "_shared", "h1", [1.0, 0.0, 0.0, 0.0])
        engine.remove("m1")
        engine.remove("never-existed")  # 不存在 ⇒ no-op 不抛
        engine.commit()
        assert engine.entries() == {}

    def test_knn_converts_l2_to_cosine(self, tmp_path: Path):
        """vec0 MATCH 返回归一化向量的 L2 距离；引擎负责换算 cos = 1 − d²/2。"""
        engine = make_engine(tmp_path)
        engine.open()
        engine.upsert("same", "r1", "_shared", "h1", [1.0, 0.0, 0.0, 0.0])
        engine.upsert("orth", "r2", "_shared", "h2", [0.0, 1.0, 0.0, 0.0])
        engine.commit()
        hits = engine.knn([1.0, 0.0, 0.0, 0.0], 2)
        assert [h[0] for h in hits] == ["same", "orth"]
        assert hits[0][2] == pytest.approx(1.0)  # 同向余弦 1（L2 距离 0）
        # 正交余弦 0（L2 距离 √2）；float32 序列化量化留 ~1e-8 级残差
        assert hits[1][2] == pytest.approx(0.0, abs=1e-6)

    def test_knn_empty_returns_empty(self, tmp_path: Path):
        engine = make_engine(tmp_path)
        engine.open()
        assert engine.knn([1.0, 0.0, 0.0, 0.0], 5) == []

    def test_knn_clamps_k_to_total(self, tmp_path: Path):
        """k 大于库内条数时按 total 收口（协议把 k 收口职责收进引擎）。"""
        engine = make_engine(tmp_path)
        engine.open()
        engine.upsert("m1", "r1", "_shared", "h1", [1.0, 0.0, 0.0, 0.0])
        engine.upsert("m2", "r2", "_shared", "h2", [0.9, 0.1, 0.0, 0.0])
        engine.commit()
        assert len(engine.knn([1.0, 0.0, 0.0, 0.0], 99)) == 2

    def test_rebuild_replaces_all_rows(self, tmp_path: Path):
        """rebuild 整体重写：旧行（含向量孤儿）全数消失，只剩新集合。"""
        engine = make_engine(tmp_path)
        engine.open()
        engine.upsert("old1", "r1", "_shared", "h1", [1.0, 0.0, 0.0, 0.0])
        engine.commit()
        engine.rebuild([("new1", "n1", "_shared", "nh1", [0.0, 0.0, 0.0, 1.0])])
        assert engine.open() is True  # rebuild 作废旧连接，动词前重开（编排层同款时序）
        assert engine.entries() == {"new1": ("nh1", "n1")}
        hits = engine.knn([1.0, 0.0, 0.0, 0.0], 5)
        assert [h[0] for h in hits] == ["new1"]  # old1 的向量行已随 DROP 消失

    def test_data_persists_and_reopens_after_close(self, tmp_path: Path):
        """commit 落盘跨实例可见；close 释放句柄后重开数据仍在（db 丢失测试的前置）。"""
        path = tmp_path / "vectors.db"
        first = SqliteVecEngine(path, dim=4)
        first.open()
        first.upsert("m1", "r1", "_shared", "h1", [1.0, 0.0, 0.0, 0.0])
        first.commit()
        first.close()
        second = SqliteVecEngine(path, dim=4)
        assert second.open() is True
        assert second.entries() == {"m1": ("h1", "r1")}


class TestVectorIndexOrchestration:
    """编排层不变量钉（fake 引擎）：对账 diff / 降级 / batch 暂存——换引擎不得漂移。"""

    def _root(self, tmp_path: Path) -> Path:
        root = tmp_path / "memroot"
        (root / "namespaces" / "_shared" / "fact").mkdir(parents=True)
        return root

    def test_reconcile_encodes_only_diff(self, tmp_path: Path):
        """带外对账只编码新增条目：未变更的存量零编码零重写（增量对账不变量）。"""
        root = self._root(tmp_path)
        fact_dir = root / "namespaces" / "_shared" / "fact"
        embedder = CountingEmbedder(unit_embedder())
        mem1, mem2 = make_mem("m1", "redis persistence"), make_mem("m2", "docker prune")
        mems = {"m1": (mem1, "_shared/fact/m1.md")}
        engine = FakeEngine(vec_db_path(root))
        vi = make_vi(root, mems, engine, embedder)
        vi.sync(mem1, "_shared/fact/m1.md")  # 建基线
        assert engine.rebuilds == [["m1"]]
        # 带外新增：目录 mtime 拨到未来 ⇒ 新于 db 基线（绕开文件系统同秒粒度）
        _shift_mtime(fact_dir, 3600)
        mems["m2"] = (mem2, "_shared/fact/m2.md")
        vi.knn([1.0, 0.0, 0.0, 0.0], k=8)  # 读路径触发 stale 对账
        assert engine.upserts == ["m2"]  # 只 upsert 新增那条，m1 不重写
        assert embedder.calls == 2  # 基线 rebuild 编码 m1 一次 + 对账只编码新增 m2 一次

    def test_reconcile_applies_move_and_remove(self, tmp_path: Path):
        """对账三 diff 的另外两支：移除消失条目、修正挪位 rel_path，均零编码。"""
        root = self._root(tmp_path)
        fact_dir = root / "namespaces" / "_shared" / "fact"
        embedder = CountingEmbedder(unit_embedder())
        m1, m2 = make_mem("m1", "redis persistence"), make_mem("m2", "memcached threads")
        mems = {
            "m1": (m1, "_shared/fact/m1.md"),
            "m2": (m2, "_shared/fact/m2.md"),
        }
        engine = FakeEngine(vec_db_path(root))
        vi = make_vi(root, mems, engine, embedder)
        vi.sync(m1, "_shared/fact/m1.md")
        vi.sync(m2, "_shared/fact/m2.md")  # 基线：首次 sync 的 rebuild(scan_pairs) 已含两条
        assert engine.upserts == []  # 两条 sync 均经 entries 快照 hash 命中，零 upsert
        # 带外：m2 文件删除、m1 挪到 episode 目录（内容不变）
        del mems["m2"]
        mems["m1"] = (m1, "_shared/episode/m1.md")
        _shift_mtime(fact_dir, 3600)
        vi.knn([1.0, 0.0, 0.0, 0.0], k=8)
        assert engine.removes == ["m2"]
        assert engine.rel_updates == [("m1", "_shared/episode/m1.md")]
        assert engine.upserts == []  # 对账零新增 upsert（内容均未变）

    def test_engine_unavailable_degrades_every_verb(self, tmp_path: Path):
        """引擎不可用（open=False，等价未装 sqlite-vec）：全部动词 no-op 降级，
        且 rebuild 不得先编码——引擎侧守卫在编码之前（宁缺勿炸）。"""
        root = self._root(tmp_path)
        embedder = CountingEmbedder(unit_embedder())
        mem1 = make_mem("m1", "redis persistence")
        engine = FakeEngine(vec_db_path(root), open_ok=False)
        vi = make_vi(root, {}, engine, embedder)
        vi.sync(mem1, "_shared/fact/m1.md")
        assert engine.upserts == [] and engine.commits == 0 and engine.rebuilds == []
        assert vi.knn([1.0, 0.0, 0.0, 0.0], k=5) == []
        assert vi.rebuild([(mem1, "_shared/fact/m1.md")]) == {"skipped": 1}
        assert embedder.calls == 0  # 引擎不可用 ⇒ 不先编码

    def test_batch_defers_and_flushes_once(self, tmp_path: Path):
        """batch 批内只暂存（引擎零调用），批尾一次批量编码 + 单次 commit 落库。"""
        root = self._root(tmp_path)
        embedder = CountingEmbedder(unit_embedder())
        m1, m2 = make_mem("m1", "redis"), make_mem("m2", "nginx")
        m3 = make_mem("m3", "docker", archived=True)  # 批内归档 ⇒ flush 时移除
        engine = FakeEngine(vec_db_path(root))
        vi = make_vi(root, {}, engine, embedder)
        vi.defer()
        vi.sync(m1, "_shared/fact/m1.md")
        vi.sync(m2, "_shared/fact/m2.md")
        vi.sync(m1, "_shared/fact/m1.md")  # 同 id 重复：批内去重
        vi.sync(m3, "archive/fact/m3.md")
        assert engine.upserts == [] and engine.removes == [] and engine.commits == 0
        assert embedder.calls == 0  # 批内零编码
        vi.flush_pending()
        assert set(engine.upserts) == {"m1", "m2"}
        assert engine.removes == ["m3"]
        assert engine.commits == 1  # 批尾一次落库
        assert embedder.calls == 1  # 批量编码收拢成一次调用

    def test_flush_skips_unchanged_content(self, tmp_path: Path):
        """批内内容未变（hash 相同）⇒ flush 零重编码零重写——feedback 零成本不变量。"""
        root = self._root(tmp_path)
        embedder = CountingEmbedder(unit_embedder())
        m1 = make_mem("m1", "redis persistence")
        engine = FakeEngine(vec_db_path(root))
        vi = make_vi(root, {"m1": (m1, "_shared/fact/m1.md")}, engine, embedder)
        vi.sync(m1, "_shared/fact/m1.md")  # 建基线（rebuild 编码 1 次）
        assert engine.rebuilds == [["m1"]] and embedder.calls == 1
        vi.defer()
        vi.sync(m1, "_shared/fact/m1.md")
        vi.flush_pending()
        assert engine.upserts == []  # hash 未变 ⇒ 不重写
        assert embedder.calls == 1  # 零重编码
        assert engine.commits == 2  # sync 一次 + flush 尾一次（无条件 commit 语义）

    def test_engine_fault_swallowed_then_recovers(self, tmp_path: Path):
        """引擎故障抛 VectorEngineError ⇒ 编排层静默吞（宁缺勿炸）；下次写动词照常重试。"""
        root = self._root(tmp_path)
        embedder = CountingEmbedder(unit_embedder())
        m1, m2 = make_mem("m1", "redis"), make_mem("m2", "nginx")
        engine = FakeEngine(vec_db_path(root))
        vi = make_vi(root, {"m1": (m1, "_shared/fact/m1.md")}, engine, embedder)
        vi.sync(m1, "_shared/fact/m1.md")  # 基线
        engine.fail_on = "upsert"
        vi.sync(m2, "_shared/fact/m2.md")  # upsert 故障 ⇒ 静默降级
        assert "m2" not in engine.meta
        assert engine.commits == 1  # 故障路径不新增 commit
        engine.fail_on = None
        vi.sync(m2, "_shared/fact/m2.md")  # 重试恢复
        assert "m2" in engine.meta
