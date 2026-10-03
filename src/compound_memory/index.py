"""Index: token→path 的可重建缓存（纯路径集合视图）。

不变量在此唯一归属：活动记忆必被索引，归档记忆必不在索引。
缓存文件缺失或损坏 ⇒ 经注入的 scan_pairs 全量重建——降级到慢，绝不报错。
活性是 store 级而非进程级：读路径检测跨进程缓存更新（重载）与带外目录
变更（增量对账，只应用 diff）；手编已有文件的内容不改目录 mtime，那条路走显式 rebuild。
检索文本知识来自 scoring.doc_text（单一定义点）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .model import Memory
from .scoring import tokenize, doc_text


class Index:
    """Deep module: 三个动词 sync / candidates / rebuild，缓存机制全部在实现内。

    调用方无需感知缓存何时加载、何时重建、archived 走哪条路，
    也无需感知缓存是否被其他进程更新过——活性检测在读路径内部完成。
    """

    def __init__(self, root: Path, scan_pairs: Callable[[], list[tuple[Memory, str]]]) -> None:
        self._root = root
        self._scan_pairs = scan_pairs
        self._dir = root / "index"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "tokens.json"
        self._data: dict[str, list[str]] | None = None  # None = 未加载
        self._dead = False  # 落盘缓存缺失或损坏，待重建
        self._loaded_stamp: int | None = None  # 缓存文件上次加载时的 mtime_ns

    # ---------- 缓存活性（重建/重载协议在这里，调用方不可见） ----------

    def _load(self) -> dict[str, list[str]]:
        if self._data is None:
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
                self._loaded_stamp = self._cache_stamp()
            except (OSError, json.JSONDecodeError):
                self._data = {}
                self._dead = True
        return self._data

    def _cache_stamp(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _ensure_live(self) -> None:
        """写路径保活：缓存缺失/损坏 ⇒ 重建（随后的增量 upsert 基于活缓存）。"""
        self._load()
        if self._dead or not self._path.exists():
            self.rebuild(self._scan_pairs())

    def _ensure_fresh(self) -> None:
        """读路径三级自愈（原 Q1-A 决策的跨进程扩展）：

        1. 缺失/损坏 ⇒ 全量重建；
        2. 缓存文件 mtime 变了（其他进程写过）⇒ 丢弃内存态重载；
        3. 任一 ns/type 目录 mtime 晚于缓存文件（带外新增/删除 .md）
           ⇒ 增量对账：scan 后只更新 diff 的条目（与 VectorIndex._reconcile
           同构，perf-bench：千条库带外写后首查的大头）。
        """
        self._ensure_live()
        stamp = self._cache_stamp()
        if self._loaded_stamp is not None and stamp != self._loaded_stamp:
            self._data = None
            self._load()
            if self._dead:
                self.rebuild(self._scan_pairs())
                return
            stamp = self._loaded_stamp if self._loaded_stamp is not None else stamp
        if stamp is not None and self._dirs_newer_than(stamp):
            self._reconcile()

    def _dirs_newer_than(self, cache_stamp: int) -> bool:
        """活动区目录在缓存落盘后发生过增删（新增/删除文件会更新父目录 mtime）。"""
        ns_root = self._root / "namespaces"
        if not ns_root.is_dir():
            return False
        try:
            ns_dirs = [d for d in ns_root.iterdir() if d.is_dir()]
        except OSError:
            return False
        for ns_dir in ns_dirs:
            try:
                if ns_dir.stat().st_mtime_ns > cache_stamp:
                    return True
                type_dirs = [d for d in ns_dir.iterdir() if d.is_dir()]
            except OSError:
                continue
        for t_dir in type_dirs:
            try:
                if t_dir.stat().st_mtime_ns > cache_stamp:
                    return True
            except OSError:
                continue
        return False

    def _reconcile(self) -> None:
        """带外增删的增量对账：反转缓存出 rel→tokens 基线，scan 后只应用 diff。

        与 VectorIndex._reconcile 同构——带外写一条不再放大成全库重建
        （scan + tokenize + tokens.json 全量重写，千条库秒级）。diff 应用在
        内存态完成后一次落盘；缓存与全量重建是集合等价的（rels 列表顺序
        不保证一致，candidates 的 sorted 输出不受影响）。
        """
        index = self._load()
        known: dict[str, set[str]] = {}
        for tok, rels in index.items():
            for rel in rels:
                known.setdefault(rel, set()).add(tok)
        active: dict[str, set[str]] = {}
        for mem, rel in self._scan_pairs():
            if not mem.archived:
                active[rel] = set(tokenize(doc_text(mem)))
        changed = False
        for rel, tokens in active.items():
            if known.get(rel) == tokens:
                continue
            self._purge(rel)
            for tok in tokens:
                index.setdefault(tok, []).append(rel)
            changed = True
        for rel in known:
            if rel not in active:
                self._purge(rel)
                changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        # 目录可能被外部整体移走（测试模拟缓存丢失、或人为 rm -rf index/），写前确保存在
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data or {}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)
        self._loaded_stamp = self._cache_stamp()  # 自己写盘后刷新基线，避免自触发重载

    # ---------- interface ----------

    def sync(self, mem: Memory, rel_path: str) -> None:
        """使索引与 mem 一致：active ⇒ rel_path 已索引；archived ⇒ rel_path 已移除。

        rel_path 指该记忆在活动区的路径；归档场景由调用方传入原活动路径。
        """
        self._ensure_live()
        if mem.archived:
            self._remove(rel_path)
        else:
            self._upsert(mem, rel_path)

    def candidates(self, tokens: list[str]) -> list[str]:
        """文档含任一 query token 的相对路径；[] 表示无匹配。

        读路径三级自愈（跨进程重载 / 带外重建）在内部完成，调用方无感。
        """
        self._ensure_fresh()
        index = self._load()
        rels: set[str] = set()
        for tok in tokens:
            rels.update(index.get(tok, []))
        return sorted(rels)

    def rebuild(self, memories: list[tuple[Memory, str]]) -> dict[str, int]:
        """以 (memory, rel_path) 序对全量重建；返回计数。"""
        index: dict[str, list[str]] = {}
        for mem, rel in memories:
            for tok in set(tokenize(doc_text(mem))):
                index.setdefault(tok, []).append(rel)
        self._data = index
        self._dead = False
        self._save()
        return {"memories": len(memories), "tokens": len(index)}

    # ---------- 内部：变更原语 ----------

    def _purge(self, rel_path: str) -> None:
        """清除某条路径的全部词条残留（不落盘，由调用方决定后续写）。"""
        index = self._load()
        for tok in list(index):
            if rel_path in index[tok]:
                index[tok].remove(rel_path)
                if not index[tok]:
                    del index[tok]

    def _upsert(self, mem: Memory, rel_path: str) -> None:
        """加入/刷新一条记忆的词条；先移除其残留旧路径。"""
        self._purge(rel_path)
        index = self._load()
        for tok in set(tokenize(doc_text(mem))):
            index.setdefault(tok, []).append(rel_path)
        self._save()

    def _remove(self, rel_path: str) -> None:
        self._purge(rel_path)
        self._save()
