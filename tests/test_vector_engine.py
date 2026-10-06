"""VecEngine 引擎缝测试：SqliteVecEngine 直测（契约面：往返/knn 换算/rebuild/持久）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from compound_memory.vector_engine import SqliteVecEngine


def make_engine(tmp_path: Path, dim: int = 4) -> SqliteVecEngine:
    return SqliteVecEngine(tmp_path / "vectors.db", dim=dim)


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
