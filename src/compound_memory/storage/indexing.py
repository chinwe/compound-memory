"""索引动词（动词件，#37）：sync_indexes / scan_pairs / rebuild_index。

「全部写路径的索引收口」不变量的物理单点（ADR 0003 模块表）：词法 + 向量
两份缓存一起保活。facade 保留三个薄委托（_sync_indexes 供 write/feedback/
revive/_archive/_move_to_active 调用；_scan_pairs 是 Index/VectorIndex 构造
注入的回调与 tests 的触达面；rebuild_index 是公开动词）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Protocol

from ..index import Index
from ..model import Memory
from ..vector_index import VectorIndex


class IndexingDeps(Protocol):
    """indexing 动词实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。"""

    root: Path
    ns_root: Path
    index: Index
    vector_index: VectorIndex

    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]: ...


def sync_indexes(store: IndexingDeps, mem: Memory, rel_path: str) -> None:
    """全部写路径的索引收口：词法 + 向量两份缓存一起保活（向量侧 hash 未变时零编码）。"""
    store.index.sync(mem, rel_path)
    store.vector_index.sync(mem, rel_path)


def scan_pairs(store: IndexingDeps) -> list[tuple[Memory, str]]:
    """扫描活动区供 Index 全量重建（注入回调，惰性调用）。"""
    return [
        (mem, path.relative_to(store.root).as_posix())
        for mem, path in store._scan_parsed(store.ns_root)
    ]


def rebuild_index(store: IndexingDeps) -> dict[str, Any]:
    counts = store.index.rebuild(scan_pairs(store))
    counts.update(store.vector_index.rebuild(scan_pairs(store)))
    return counts
