"""索引动词（动词件，#37）：sync_indexes / scan_pairs / rebuild_index。

「全部写路径的索引收口」不变量的物理单点（ADR 0003 模块表）：词法 + 向量
两份缓存一起保活。facade 保留三个薄委托（_sync_indexes 供 write/feedback/
revive/_archive/_move_to_active 调用；_scan_pairs 是 Index/VectorIndex 构造
注入的回调与 tests 的触达面；rebuild_index 是公开动词）。

scan 的共享协议（#41）：读动词内两份缓存各自对账共用一遍 scan（ScanWindow，
窗口由 search 开启）；显式 rebuild 恒走 fresh scan（手编内容后的补救路径
不能吃缓存 scan——手编不改目录 mtime）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Protocol

from ..index import Index
from ..liveness import ScanWindow
from ..model import Memory
from ..vector_index import VectorIndex


class IndexingDeps(Protocol):
    """indexing 动词实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。"""

    root: Path
    ns_root: Path
    index: Index
    vector_index: VectorIndex
    _scan_window: ScanWindow

    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]: ...


def sync_indexes(store: IndexingDeps, mem: Memory, rel_path: str) -> None:
    """全部写路径的索引收口：词法 + 向量两份缓存一起保活（向量侧 hash 未变时零编码）。"""
    store.index.sync(mem, rel_path)
    store.vector_index.sync(mem, rel_path)


def _scan_all(store: IndexingDeps) -> list[tuple[Memory, str]]:
    """全量扫描活动区（fresh scan 的本体：rglob + 容错 parse）。"""
    return [
        (mem, path.relative_to(store.root).as_posix())
        for mem, path in store._scan_parsed(store.ns_root)
    ]


def scan_pairs(store: IndexingDeps) -> list[tuple[Memory, str]]:
    """扫描活动区供 Index/VectorIndex 保活与对账（注入回调，惰性调用）。

    读动词窗口内复用同一份结果（#41 共享 scan）；窗口外等价 fresh scan。
    """
    return store._scan_window.pairs(lambda: _scan_all(store))


def rebuild_index(store: IndexingDeps) -> dict[str, Any]:
    """全量重建两份缓存：共享一遍 fresh scan（绕开 ScanWindow——手编内容
    后的补救路径不能吃缓存 scan），两份缓存不再各扫一遍。"""
    memories = _scan_all(store)
    counts = store.index.rebuild(memories)
    counts.update(store.vector_index.rebuild(memories))
    return counts
