"""Index: token→path 的可重建缓存（纯路径集合视图）。

不变量在此唯一归属：活动记忆必被索引，归档记忆必不在索引。
缓存文件缺失或损坏 ⇒ 经注入的 scan_pairs 全量重建——降级到慢，绝不报错。
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

    调用方无需感知缓存何时加载、何时重建、archived 走哪条路。
    """

    def __init__(self, root: Path, scan_pairs: Callable[[], list[tuple[Memory, str]]]) -> None:
        self._root = root
        self._scan_pairs = scan_pairs
        self._dir = root / "index"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "tokens.json"
        self._data: dict[str, list[str]] | None = None  # None = 未加载
        self._dead = False  # 落盘缓存缺失或损坏，待重建

    # ---------- 缓存活性（重建协议在这里，调用方不可见） ----------

    def _load(self) -> dict[str, list[str]]:
        if self._data is None:
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}
                self._dead = True
        return self._data

    def _ensure_live(self) -> None:
        """写路径保持缓存存活（原 Q1-A 决策）；读取路径同样自愈。"""
        self._load()
        if self._dead or not self._path.exists():
            self.rebuild(self._scan_pairs())

    def _save(self) -> None:
        # 目录可能被外部整体移走（测试模拟缓存丢失、或人为 rm -rf index/），写前确保存在
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data or {}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

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
        """文档含任一 query token 的相对路径；[] 表示无匹配。"""
        self._ensure_live()
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

    def _upsert(self, mem: Memory, rel_path: str) -> None:
        """加入/刷新一条记忆的词条；先移除其残留旧路径。"""
        index = self._load()
        for tok in list(index):
            if rel_path in index[tok]:
                index[tok].remove(rel_path)
                if not index[tok]:
                    del index[tok]
        for tok in set(tokenize(doc_text(mem))):
            index.setdefault(tok, []).append(rel_path)
        self._save()

    def _remove(self, rel_path: str) -> None:
        index = self._load()
        for tok in list(index):
            if rel_path in index[tok]:
                index[tok].remove(rel_path)
                if not index[tok]:
                    del index[tok]
        self._save()
