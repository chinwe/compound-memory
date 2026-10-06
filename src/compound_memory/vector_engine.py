"""VecEngine 引擎缝：向量存储引擎协议与 sqlite-vec 实现（ADR 0004 / issue #40）。

引擎只存不算：content-hash 计算与 embedder 调用留在编排层（vector_index），
本模块只负责向量行与 meta 影子表的存取——vec0 虚表、serialize_float32、
L2 距离→余弦换算全在此单点。换引擎（faiss/lancedb/纯 numpy）实现同一协议
即可，不波及编排不变量；两缝正交（模型缝在 embedding.py，本模块不动）。

三层降级守卫中，SQLITE_VEC_OK（sqlite-vec 依赖是否可导入）归属本模块：
依赖缺失时 open() 恒 False，编排层据此整体降级纯词面（VEC_AVAILABLE 在
embedding.py；embedder None 在编排层 _usable()）。

故障语义（宁缺勿炸）：任何存储故障由引擎自回收——关闭并清空缓存连接，
抛 VectorEngineError 交编排层静默降级；下次操作经 open() 重开（损坏的 db
由读路径 rebuild 重建）。引擎不建父目录：db 路径目录由词法缓存侧先建。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import ModuleType
from typing import NoReturn, Protocol

from .embedding import EMBED_DIM

try:
    import sqlite_vec

    SQLITE_VEC_OK = True
except ImportError:  # pragma: no cover - 取决于安装环境是否带 vec extra
    SQLITE_VEC_OK = False


class VectorEngineError(Exception):
    """引擎存储层故障（sqlite/IO 错误的包装）；编排层捕获后静默降级。"""


# rebuild 整体重写的行形状：(mem_id, rel_path, ns, content_hash, vec)
EngineRow = tuple[str, str, str, str, list[float]]

_META_COLUMNS = (
    "mem_id TEXT PRIMARY KEY, rel_path TEXT NOT NULL, ns TEXT NOT NULL, "
    "content_hash TEXT NOT NULL, vec_row INTEGER NOT NULL"
)


def _load_api() -> ModuleType:
    """取 sqlite_vec 模块；依赖缺失抛 VectorEngineError（引擎侧守卫单点）。"""
    if not SQLITE_VEC_OK:
        raise VectorEngineError("sqlite-vec dependency not installed")
    import sqlite_vec

    return sqlite_vec


class VecEngine(Protocol):
    """向量存储引擎协议（只存不算；hash 比对与编码决策在编排层）。

    生命周期约定：动词（upsert/remove/update_rel/entries/knn/commit）须在
    open() 返回 True 之后调用；存储故障一律自回收连接并抛 VectorEngineError。
    """

    def open(self) -> bool:
        """确保引擎就绪（惰性建连 + 建表）；不可用（依赖缺失/建连失败）返回 False。"""
        ...

    def close(self) -> None:
        """释放连接（Windows 上持有句柄会锁 db 文件，删/挪 db 前先关）。"""
        ...

    def upsert(self, mem_id: str, rel_path: str, ns: str, content_hash: str, vec: list[float]) -> None:
        """整体替换一行：删旧向量行、写新向量与 meta（是否值得重写由编排层裁决）。"""
        ...

    def remove(self, mem_id: str) -> None:
        """删除一行（向量 + meta）；不存在则 no-op。"""
        ...

    def update_rel(self, mem_id: str, rel_path: str) -> None:
        """仅修正 rel_path（内容未变的手编挪位）。"""
        ...

    def entries(self) -> dict[str, tuple[str, str]]:
        """meta 快照：mem_id -> (content_hash, rel_path)——编排层对账 diff 的基线。"""
        ...

    def knn(self, query_vec: list[float], k: int) -> list[tuple[str, str, float]]:
        """KNN：[(mem_id, rel_path, cosine)]；引擎内完成 L2→余弦换算与 k 收口。"""
        ...

    def rebuild(self, rows: list[EngineRow]) -> None:
        """DROP 后全量重写；成功后旧缓存连接作废（下次 open 重开拿到新表）。"""
        ...

    def commit(self) -> None:
        """提交当前事务。"""
        ...


class SqliteVecEngine:
    """VecEngine 的 sqlite-vec 实现：vec0 虚表 + meta 影子表（自 vector_index 迁入）。"""

    def __init__(self, path: Path, dim: int = EMBED_DIM) -> None:
        self._path = path
        self._dim = dim
        self._db: sqlite3.Connection | None = None

    # ---------- 连接生命周期 ----------

    def open(self) -> bool:
        """惰性建连 + 建表；损坏/缺失交编排层 rebuild 路径，仍失败则视为不可用。"""
        if not SQLITE_VEC_OK:
            return False
        if self._db is not None:
            return True
        db: sqlite3.Connection | None = None
        try:
            api = _load_api()
            db = sqlite3.connect(self._path)
            db.enable_load_extension(True)
            api.load(db)
            db.enable_load_extension(False)
            db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vecs USING vec0(embedding float[{self._dim}])")
            db.execute(f"CREATE TABLE IF NOT EXISTS meta ({_META_COLUMNS})")
            self._db = db
            return True
        except sqlite3.DatabaseError:
            self._close_quietly(db)  # 建连成功但建表失败的半开连接不留句柄
            return False

    def close(self) -> None:
        self._close_quietly(self._db)
        self._db = None

    # ---------- 存取动词 ----------

    def upsert(self, mem_id: str, rel_path: str, ns: str, content_hash: str, vec: list[float]) -> None:
        api = _load_api()
        db = self._require()
        try:
            row = db.execute("SELECT vec_row FROM meta WHERE mem_id = ?", (mem_id,)).fetchone()
            if row is not None:
                db.execute("DELETE FROM vecs WHERE rowid = ?", (row[0],))
            cur = db.execute(
                "INSERT INTO vecs(rowid, embedding) VALUES (?, ?)", (None, api.serialize_float32(vec))
            )
            db.execute(
                "INSERT INTO meta(mem_id, rel_path, ns, content_hash, vec_row) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(mem_id) DO UPDATE SET rel_path = ?, content_hash = ?, vec_row = ?",
                (mem_id, rel_path, ns, content_hash, cur.lastrowid, rel_path, content_hash, cur.lastrowid),
            )
        except sqlite3.DatabaseError as exc:
            self._fail(exc)

    def remove(self, mem_id: str) -> None:
        db = self._require()
        try:
            row = db.execute("SELECT vec_row FROM meta WHERE mem_id = ?", (mem_id,)).fetchone()
            if row is not None:
                db.execute("DELETE FROM vecs WHERE rowid = ?", (row[0],))
                db.execute("DELETE FROM meta WHERE mem_id = ?", (mem_id,))
        except sqlite3.DatabaseError as exc:
            self._fail(exc)

    def update_rel(self, mem_id: str, rel_path: str) -> None:
        db = self._require()
        try:
            db.execute("UPDATE meta SET rel_path = ? WHERE mem_id = ?", (rel_path, mem_id))
        except sqlite3.DatabaseError as exc:
            self._fail(exc)

    def entries(self) -> dict[str, tuple[str, str]]:
        db = self._require()
        try:
            return {
                mid: (content_hash, rel_path)
                for mid, content_hash, rel_path in db.execute(
                    "SELECT mem_id, content_hash, rel_path FROM meta"
                )
            }
        except sqlite3.DatabaseError as exc:
            self._fail(exc)

    def knn(self, query_vec: list[float], k: int) -> list[tuple[str, str, float]]:
        """vec0 MATCH 返回归一化向量的 L2 距离，cos = 1 − d²/2。"""
        api = _load_api()
        db = self._require()
        try:
            total = db.execute("SELECT count(*) FROM meta").fetchone()[0]
            if total == 0:
                return []
            rows = db.execute(
                "SELECT m.mem_id, m.rel_path, v.distance FROM vecs v "
                "JOIN meta m ON m.vec_row = v.rowid "
                "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                (api.serialize_float32(query_vec), min(k, total)),
            ).fetchall()
            return [(mid, rel, 1.0 - (d ** 2) / 2.0) for mid, rel, d in rows]
        except sqlite3.DatabaseError as exc:
            self._fail(exc)

    def rebuild(self, rows: list[EngineRow]) -> None:
        """DROP + 全量重写：独立连接完成，成功后旧缓存连接一并作废。"""
        api = _load_api()
        fresh: sqlite3.Connection | None = None
        try:
            fresh = sqlite3.connect(self._path)
            fresh.enable_load_extension(True)
            api.load(fresh)
            fresh.enable_load_extension(False)
            fresh.execute("DROP TABLE IF EXISTS meta")
            fresh.execute("DROP TABLE IF EXISTS vecs")
            fresh.execute(f"CREATE VIRTUAL TABLE vecs USING vec0(embedding float[{self._dim}])")
            fresh.execute(f"CREATE TABLE meta ({_META_COLUMNS})")
            for mem_id, rel_path, ns, content_hash, vec in rows:
                cur = fresh.execute(
                    "INSERT INTO vecs(rowid, embedding) VALUES (?, ?)", (None, api.serialize_float32(vec))
                )
                fresh.execute(
                    "INSERT INTO meta(mem_id, rel_path, ns, content_hash, vec_row) VALUES (?, ?, ?, ?, ?)",
                    (mem_id, rel_path, ns, content_hash, cur.lastrowid),
                )
            fresh.commit()
            fresh.close()
            fresh = None
            self.close()  # 重建用了独立连接，旧缓存引用一并作废（下次 open 重开）
        except sqlite3.DatabaseError:
            if fresh is not None:
                self._close_quietly(fresh)
            self.close()
            raise VectorEngineError("rebuild failed") from None

    def commit(self) -> None:
        db = self._require()
        try:
            db.commit()
        except sqlite3.DatabaseError as exc:
            self._fail(exc)

    # ---------- 内部 ----------

    def _require(self) -> sqlite3.Connection:
        if self._db is None:
            raise VectorEngineError("engine not open")
        return self._db

    def _fail(self, exc: Exception) -> NoReturn:
        """故障自回收（旧编排层 _discard 语义随引擎走）：关连接清引用再抛包装错误。"""
        self.close()
        raise VectorEngineError(str(exc)) from exc

    @staticmethod
    def _close_quietly(db: sqlite3.Connection | None) -> None:
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
