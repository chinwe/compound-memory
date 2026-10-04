"""VectorIndex: 记忆向量缓存（sqlite-vec vec0 表），Index 的姊妹缓存。

不变量与词法 Index 同构（spec「索引即缓存」）：
- 活动记忆必被索引（内容 hash 变更才重编码——feedback 只动 conf/uses，零编码成本）；
- 归档记忆必不在索引；
- 缓存缺失 ⇒ 全量重建（无 diff 基线）；带外增删 ⇒ 增量对账，只编码 diff——
  跨进程小写入（另一宿主 write/feedback 一条）不再放大成全库重编码。

降级契约：embedder=None（未装 vec extra / 模型缺失）时全部动词退化为 no-op /
空结果，检索自动退纯词面——检索降级不报错。写入路径同样吞缓存故障（索引可
重建，宁缺勿炸 write）；重建本身再失败才保持 no-op，等待下次读路径重试。

换 embedding 模型属运维动作：需显式 `rebuild-index`（hash 只校验内容，不校验模型）。
模型 repo id 与输出维度（建表维度）经 embedding.py 的环境变量解析（单一定义点），
换模型须同步 COMPOUND_MEMORY_EMBEDDING_DIM。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Callable

from .embedding import EMBED_DIM
from .model import Memory
from .scoring import doc_text

try:
    import sqlite_vec

    SQLITE_VEC_OK = True
except ImportError:  # pragma: no cover - 取决于安装环境是否带 vec extra
    SQLITE_VEC_OK = False

DB_NAME = "vectors.db"
# 带外增删检测的目录 mtime 粒度与词法 Index 一致（新增/删除 .md 会更新 ns/type 目录 mtime）


def _content_hash(mem: Memory) -> str:
    return hashlib.sha256(doc_text(mem).encode("utf-8")).hexdigest()[:16]


class VectorIndex:
    """Deep module：sync / knn / rebuild 三个动词，缓存机制与降级全部在实现内。"""

    def __init__(
        self,
        root: Path,
        scan_pairs: Callable[[], list[tuple[Memory, str]]],
        embedder: Callable[[list[str]], list[list[float]]] | None,
    ) -> None:
        self._root = root
        self._scan_pairs = scan_pairs
        self._embedder = embedder
        self._path = root / "index" / DB_NAME
        self._db: sqlite3.Connection | None = None
        self._rebuilt_stamp: int | None = None  # 自己重建后落盘的 db mtime 基线

    # ---------- 降级与活性 ----------

    def _usable(self) -> bool:
        return SQLITE_VEC_OK and self._embedder is not None

    def _connect(self) -> sqlite3.Connection | None:
        """惰性建连 + 建表；损坏/缺失交 _ensure_fresh 重建，仍失败则视为不可用。"""
        if self._db is not None:
            return self._db
        if not self._usable():
            return None
        try:
            db = sqlite3.connect(self._path)
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vecs USING vec0(embedding float[{EMBED_DIM}])")
            db.execute(
                "CREATE TABLE IF NOT EXISTS meta ("
                "mem_id TEXT PRIMARY KEY, rel_path TEXT NOT NULL, ns TEXT NOT NULL, "
                "content_hash TEXT NOT NULL, vec_row INTEGER NOT NULL)"
            )
            self._db = db
            return db
        except sqlite3.DatabaseError:
            return None

    def _ensure_live(self) -> None:
        """写路径保活：db 缺失 ⇒ 重建（不做目录 mtime 检查——write 先落文件再 sync，
        目录 mtime 必然新于 db，做检查会导致每次写入都全量重建）。"""
        if not self._usable():
            return
        if self._db_stamp() is None:
            self.rebuild(self._scan_pairs())

    def _ensure_fresh(self) -> None:
        """读路径自愈：db 缺失 ⇒ 全量重建（无 diff 基线）；活动区带外增删 ⇒ 增量对账。

        对账失败保持静默（no-op），下次操作重试——宁缺勿炸。
        """
        if not self._usable():
            return
        stamp = self._db_stamp()
        if stamp is None:
            self.rebuild(self._scan_pairs())
        elif self._stale(stamp):
            self._reconcile()

    def _db_stamp(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _stale(self, stamp: int) -> bool:
        """db 落盘后活动区发生过带外增删（新增/删除文件会更新 ns/type 目录 mtime）。

        不做"db 未变即未 stale"的短路——手编文件只动目录 mtime 不动 db，短路会漏检。
        """
        ns_root = self._root / "namespaces"
        if not ns_root.is_dir():
            return False
        try:
            for ns_dir in ns_root.iterdir():
                if not ns_dir.is_dir():
                    continue
                if ns_dir.stat().st_mtime_ns > stamp:
                    return True
                for t_dir in ns_dir.iterdir():
                    if t_dir.is_dir() and t_dir.stat().st_mtime_ns > stamp:
                        return True
        except OSError:
            return False
        return False

    def _reconcile(self) -> None:
        """带外增删的增量对账：只编码 diff（新增/内容变更），未变更零编码。

        跨进程写入的读路径自愈从「全库重编码」降为「变更条数 × 单条编码」——
        另一宿主写一条记忆后，本进程的下一次 search 只等这一条的编码。
        已消失（删除/转入归档）的条目移除向量行；内容未变仅 rel_path 漂移
        （手编挪位）只更新 meta。故障保持静默（宁缺勿炸），下次读路径重试。
        """
        db = self._connect()
        if db is None:
            return
        try:
            active = [(mem, rel) for mem, rel in self._scan_pairs() if not mem.archived]
            known = {
                mid: (content_hash, rel_path)
                for mid, content_hash, rel_path in db.execute(
                    "SELECT mem_id, content_hash, rel_path FROM meta"
                )
            }
            active_ids = {mem.id for mem, _ in active}
            for mid in known:
                if mid not in active_ids:
                    self._remove(db, mid)
            for mem, rel in active:
                entry = known.get(mem.id)
                if entry is not None and entry[0] == _content_hash(mem):
                    if entry[1] != rel:
                        db.execute("UPDATE meta SET rel_path = ? WHERE mem_id = ?", (rel, mem.id))
                    continue
                self._upsert(db, mem, rel)
            db.commit()
            # 对账落盘后刷新基线，与 sync/rebuild 同款语义（避免自触发 stale）
            self._rebuilt_stamp = self._db_stamp()
        except (sqlite3.DatabaseError, RuntimeError, OSError):
            self._discard(db)

    # ---------- interface ----------

    def sync(self, mem: Memory, rel_path: str) -> None:
        """使向量缓存与 mem 一致：active ⇒ 已索引（hash 未变零编码）；archived ⇒ 已移除。"""
        if not self._usable():
            return
        self._ensure_live()
        db = self._connect()
        if db is None:
            return
        try:
            if mem.archived:
                self._remove(db, mem.id)
            else:
                self._upsert(db, mem, rel_path)
            db.commit()
            # 自己写盘后刷新基线，避免写路径落盘的文件/缓存 mtime 差在下次读路径
            # 被误判成"带外增删"而触发多余全量重建（与词法 Index._save 同思路）
            self._rebuilt_stamp = self._db_stamp()
        except sqlite3.DatabaseError:
            self._discard(db)

    def knn(self, query_vec: list[float], k: int) -> list[tuple[str, str, float]]:
        """KNN：[(mem_id, rel_path, cosine)]；vec0 MATCH 返回归一化向量 L2 距离，cos = 1 − d²/2。"""
        if not self._usable():
            return []
        self._ensure_fresh()
        db = self._connect()
        if db is None:
            return []
        try:
            total = db.execute("SELECT count(*) FROM meta").fetchone()[0]
            if total == 0:
                return []
            import sqlite_vec

            rows = db.execute(
                "SELECT m.mem_id, m.rel_path, v.distance FROM vecs v "
                "JOIN meta m ON m.vec_row = v.rowid "
                "WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
                (sqlite_vec.serialize_float32(query_vec), min(k, total)),
            ).fetchall()
            return [(mid, rel, 1.0 - (d ** 2) / 2.0) for mid, rel, d in rows]
        except sqlite3.DatabaseError:
            self._discard(db)
            return []

    def rebuild(self, memories: list[tuple[Memory, str]]) -> dict[str, int]:
        """全量重建（批量编码一次完成）；返回计数；不可用/失败时静默跳过。"""
        if not self._usable():
            return {"skipped": 1}
        try:
            active = [(mem, rel) for mem, rel in memories if not mem.archived]
            vectors = self._embedder([doc_text(mem) for mem, _ in active])  # type: ignore[misc]
            db = sqlite3.connect(self._path)
            db.enable_load_extension(True)
            sqlite_vec.load(db)
            db.enable_load_extension(False)
            db.execute("DROP TABLE IF EXISTS meta")
            db.execute("DROP TABLE IF EXISTS vecs")
            db.execute(f"CREATE VIRTUAL TABLE vecs USING vec0(embedding float[{EMBED_DIM}])")
            db.execute(
                "CREATE TABLE meta ("
                "mem_id TEXT PRIMARY KEY, rel_path TEXT NOT NULL, ns TEXT NOT NULL, "
                "content_hash TEXT NOT NULL, vec_row INTEGER NOT NULL)"
            )
            for (mem, rel), vec in zip(active, vectors):
                cur = db.execute("INSERT INTO vecs(rowid, embedding) VALUES (?, ?)", (None, sqlite_vec.serialize_float32(vec)))
                db.execute(
                    "INSERT INTO meta(mem_id, rel_path, ns, content_hash, vec_row) VALUES (?, ?, ?, ?, ?)",
                    (mem.id, rel, mem.ns, _content_hash(mem), cur.lastrowid),
                )
            db.commit()
            db.close()
            if self._db is not None:
                self._discard(self._db)  # 重建用了独立连接，旧引用一并作废
            self._db = None  # 下次 _connect 重开（拿到新 db）
            self._rebuilt_stamp = self._db_stamp()
            return {"memories": len(active)}
        except (sqlite3.DatabaseError, RuntimeError, OSError):
            return {"skipped": 1}

    def close(self) -> None:
        """显式释放缓存连接。Windows 上持有句柄会锁住 db 文件，
        需要删/挪 db 的调用方（测试模拟 db 丢失）先关再动。"""
        self._discard(self._db)

    # ---------- 内部 ----------

    def _upsert(self, db: sqlite3.Connection, mem: Memory, rel_path: str) -> None:
        row = db.execute("SELECT content_hash, vec_row FROM meta WHERE mem_id = ?", (mem.id,)).fetchone()
        new_hash = _content_hash(mem)
        if row is not None and row[0] == new_hash:
            db.execute("UPDATE meta SET rel_path = ? WHERE mem_id = ?", (rel_path, mem.id))
            return
        import sqlite_vec

        if row is not None:
            db.execute("DELETE FROM vecs WHERE rowid = ?", (row[1],))
        vec = self._embedder([doc_text(mem)])[0]  # type: ignore[misc]
        cur = db.execute("INSERT INTO vecs(rowid, embedding) VALUES (?, ?)", (None, sqlite_vec.serialize_float32(vec)))
        db.execute(
            "INSERT INTO meta(mem_id, rel_path, ns, content_hash, vec_row) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(mem_id) DO UPDATE SET rel_path = ?, content_hash = ?, vec_row = ?",
            (mem.id, rel_path, mem.ns, new_hash, cur.lastrowid, rel_path, new_hash, cur.lastrowid),
        )

    def _remove(self, db: sqlite3.Connection, mem_id: str) -> None:
        row = db.execute("SELECT vec_row FROM meta WHERE mem_id = ?", (mem_id,)).fetchone()
        if row is not None:
            db.execute("DELETE FROM vecs WHERE rowid = ?", (row[0],))
            db.execute("DELETE FROM meta WHERE mem_id = ?", (mem_id,))

    def _discard(self, db: sqlite3.Connection | None) -> None:
        """坏连接退路：关掉并清引用，后续操作按缺失路径重建。"""
        if db is not None:
            try:
                db.close()
            except sqlite3.Error:
                pass
        if self._db is db:
            self._db = None
