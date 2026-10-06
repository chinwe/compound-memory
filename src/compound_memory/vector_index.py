"""VectorIndex: 记忆向量缓存编排层（引擎缝之上的活性/对账/降级不变量）。

不变量与词法 Index 同构（spec「索引即缓存」）：
- 活动记忆必被索引（内容 hash 变更才重编码——feedback 只动 conf/uses，零编码成本）；
- 归档记忆必不在索引；
- 缓存缺失 ⇒ 全量重建（无 diff 基线）；带外增删 ⇒ 增量对账，只编码 diff——
  跨进程小写入（另一宿主 write/feedback 一条）不再放大成全库重编码。

引擎缝（ADR 0004 / issue #40）：存储机制下沉 vector_engine.VecEngine（引擎只
存不算），本层只做编排——content-hash 计算、embedder 调用、对账 diff、mtime
活性基线、batch 暂存都在这。engine 经构造可选参数注入（默认 SqliteVecEngine，
MemoryStore 构造参数零改动）。

降级契约：embedder=None（未装 vec extra / 模型缺失）时全部动词退化为 no-op /
空结果，检索自动退纯词面——检索降级不报错（三层守卫归属：embedder None 在
本层 _usable()；SQLITE_VEC_OK 在 vector_engine；VEC_AVAILABLE 在 embedding）。
写入路径同样吞缓存故障（索引可重建，宁缺勿炸 write）；重建本身再失败才保持
no-op，等待下次读路径重试。

换 embedding 模型属运维动作：需显式 `rebuild-index`（hash 只校验内容，不校验模型）。
模型 repo id 与输出维度（建表维度）经 embedding.py 的环境变量解析（单一定义点），
换模型须同步 COMPOUND_MEMORY_EMBEDDING_DIM。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from .liveness import dirs_newer_than
from .model import Memory
from .scoring import doc_text
from .vector_engine import SqliteVecEngine, VecEngine, VectorEngineError

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
        engine: VecEngine | None = None,
    ) -> None:
        self._root = root
        self._scan_pairs = scan_pairs
        self._embedder = embedder
        self._path = root / "index" / DB_NAME
        # 引擎缝：默认 sqlite-vec 实现。注入的引擎须在 _path 落实体文件承载
        # mtime 基线（fake 引擎在测试侧自行落标记文件）。
        self._engine: VecEngine = engine if engine is not None else SqliteVecEngine(self._path)
        self._rebuilt_stamp: int | None = None  # 自己重建后落盘的 db mtime 基线
        self._pending: list[tuple[Memory, str]] | None = None  # 非 None = batch() 批式通道中

    # ---------- 降级与活性 ----------

    def _usable(self) -> bool:
        # embedder None 守卫在本层；引擎依赖守卫（SQLITE_VEC_OK）在 engine.open()
        return self._embedder is not None

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
        if self._pending is not None:
            return  # 批内不做读路径自愈：batch 是唯一写者，批尾 flush 后恢复
        stamp = self._db_stamp()
        if stamp is None:
            self.rebuild(self._scan_pairs())
        elif dirs_newer_than(self._root / "namespaces", stamp):
            # 不做"db 未变即未 stale"的短路——手编文件只动目录 mtime 不动 db，短路会漏检
            self._reconcile()

    def defer(self) -> None:
        """批式写通道入口（仅 MemoryStore.batch 调用）：sync 暂存 pending，flush_pending 一次对账。"""
        self._pending = []

    def flush_pending(self) -> None:
        """批尾一次对账：新增/变更一次性批量编码（借 embedder 的批内分块），单次 commit。

        不可用或故障保持静默降级（宁缺勿炸），与 sync/rebuild 同一契约。
        """
        if self._pending is None:
            return
        pending, self._pending = self._pending, None
        if not pending:
            return
        if not self._usable():
            return  # 无 embedder ⇒ 整条向量路 no-op（旧 _connect 内嵌的同款守卫）
        if not self._engine.open():
            return
        try:
            known = self._engine.entries()
            seen: set[str] = set()
            changed: list[tuple[Memory, str]] = []
            for mem, rel in pending:
                if mem.archived:
                    self._engine.remove(mem.id)
                    continue
                if mem.id in seen:
                    continue
                seen.add(mem.id)
                if not self._entry_up_to_date(known, mem, rel):
                    changed.append((mem, rel))
            if changed:
                embedder = self._embedder
                assert embedder is not None
                vectors = embedder([doc_text(mem) for mem, _ in changed])
                for (mem, rel), vec in zip(changed, vectors):
                    self._engine.upsert(mem.id, rel, mem.ns, _content_hash(mem), vec)
            self._engine.commit()
            self._rebuilt_stamp = self._db_stamp()
        except (VectorEngineError, RuntimeError, OSError):
            pass  # 引擎故障已自回收连接，这里只静默降级

    def _db_stamp(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _entry_up_to_date(
        self, known: dict[str, tuple[str, str]], mem: Memory, rel: str
    ) -> bool:
        """单条缓存对账（flush_pending/_reconcile/sync 共用判定形状）：
        entries 命中且内容 hash 未变 ⇒ 条目有效，仅修 rel 漂移（update_rel）
        并返回 True；否则返回 False，由调用方走编码 upsert（sync/_reconcile
        单条现编，flush_pending 攒批批量编码）。"""
        entry = known.get(mem.id)
        if entry is not None and entry[0] == _content_hash(mem):
            if entry[1] != rel:
                self._engine.update_rel(mem.id, rel)
            return True
        return False

    def _reconcile(self) -> None:
        """带外增删的增量对账：只编码 diff（新增/内容变更），未变更零编码。

        跨进程写入的读路径自愈从「全库重编码」降为「变更条数 × 单条编码」——
        另一宿主写一条记忆后，本进程的下一次 search 只等这一条的编码。
        已消失（删除/转入归档）的条目移除向量行；内容未变仅 rel_path 漂移
        （手编挪位）只更新 meta。故障保持静默（宁缺勿炸），下次读路径重试。
        """
        if not self._usable():
            return  # 无 embedder ⇒ no-op（旧 _connect 内嵌的同款守卫）
        if not self._engine.open():
            return
        try:
            active = [(mem, rel) for mem, rel in self._scan_pairs() if not mem.archived]
            known = self._engine.entries()
            active_ids = {mem.id for mem, _ in active}
            for mid in known:
                if mid not in active_ids:
                    self._engine.remove(mid)
            for mem, rel in active:
                if not self._entry_up_to_date(known, mem, rel):
                    embedder = self._embedder
                    assert embedder is not None
                    vec = embedder([doc_text(mem)])[0]
                    self._engine.upsert(mem.id, rel, mem.ns, _content_hash(mem), vec)
            self._engine.commit()
            # 对账落盘后刷新基线，与 sync/rebuild 同款语义（避免自触发 stale）
            self._rebuilt_stamp = self._db_stamp()
        except (VectorEngineError, RuntimeError, OSError):
            pass  # 宁缺勿炸：故障静默，下次读路径重试

    # ---------- interface ----------

    def sync(self, mem: Memory, rel_path: str) -> None:
        """使向量缓存与 mem 一致：active ⇒ 已索引（hash 未变零编码）；archived ⇒ 已移除。"""
        if self._pending is not None:
            self._pending.append((mem, rel_path))  # 批内只暂存，编码与落库收拢到 flush_pending
            return
        if not self._usable():
            return
        self._ensure_live()
        if not self._engine.open():
            return
        try:
            if mem.archived:
                self._engine.remove(mem.id)
            elif not self._entry_up_to_date(self._engine.entries(), mem, rel_path):
                embedder = self._embedder
                assert embedder is not None
                vec = embedder([doc_text(mem)])[0]
                self._engine.upsert(mem.id, rel_path, mem.ns, _content_hash(mem), vec)
            self._engine.commit()
            # 自己写盘后刷新基线，避免写路径落盘的文件/缓存 mtime 差在下次读路径
            # 被误判成"带外增删"而触发多余全量重建（与词法 Index._save 同思路）
            self._rebuilt_stamp = self._db_stamp()
        except VectorEngineError:
            pass  # 引擎故障已自回收，静默降级（embedder 故障沿旧语义上抛，不在此吞）

    def knn(self, query_vec: list[float], k: int) -> list[tuple[str, str, float]]:
        """KNN：[(mem_id, rel_path, cosine)]；L2→余弦换算与 k 收口在引擎内。"""
        if not self._usable():
            return []
        self._ensure_fresh()
        if not self._engine.open():
            return []
        try:
            return self._engine.knn(query_vec, k)
        except VectorEngineError:
            return []

    def rebuild(self, memories: list[tuple[Memory, str]]) -> dict[str, int]:
        """全量重建（批量编码一次完成）；返回计数；不可用/失败时静默跳过。"""
        if not self._usable():
            return {"skipped": 1}
        if not self._engine.open():
            # 引擎不可用（依赖缺失/建连失败）在编码之前拦下——不白花编码成本
            return {"skipped": 1}
        embedder = self._embedder
        assert embedder is not None
        try:
            active = [(mem, rel) for mem, rel in memories if not mem.archived]
            vectors = embedder([doc_text(mem) for mem, _ in active])
            rows = [
                (mem.id, rel, mem.ns, _content_hash(mem), vec)
                for (mem, rel), vec in zip(active, vectors)
            ]
            self._engine.rebuild(rows)
            self._rebuilt_stamp = self._db_stamp()
            return {"memories": len(active)}
        except (VectorEngineError, RuntimeError, OSError):
            return {"skipped": 1}

    def close(self) -> None:
        """显式释放缓存连接。Windows 上持有句柄会锁住 db 文件，
        需要删/挪 db 的调用方（测试模拟 db 丢失）先关再动。"""
        self._engine.close()
