"""Index: a rebuildable token→path cache over the memory store.

Pure path-set view (Q2-B): upsert/remove/candidates on relative paths only.
Token knowledge comes from scoring.doc_text (Q3-A) — one definition point.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Memory
from .scoring import tokenize, doc_text


class Index:
    """Deep module: three-line interface, all cache mechanics inside.

    - load failure or missing file ⇒ in-memory empty; callers may rebuild.
    - candidates() returns [] when the cache can't answer — the caller decides
      whether to fall back to a directory scan.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._dir = root / "index"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "tokens.json"
        self._data: dict[str, list[str]] | None = None  # None = not loaded

    @property
    def file(self) -> Path:
        return self._path

    # ---------- persistence ----------

    def _load(self) -> dict[str, list[str]]:
        if self._data is None:
            try:
                self._data = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._data = {}
        return self._data

    def _save(self) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data or {}, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    # ---------- interface (Q2-B) ----------

    def upsert(self, mem: Memory, rel_path: str) -> None:
        """Add/refresh one memory's entries; removes any stale path first."""
        index = self._load()
        for tok in list(index):
            if rel_path in index[tok]:
                index[tok].remove(rel_path)
                if not index[tok]:
                    del index[tok]
        for tok in set(tokenize(doc_text(mem))):
            index.setdefault(tok, []).append(rel_path)
        self._save()

    def remove(self, rel_path: str) -> None:
        index = self._load()
        for tok in list(index):
            if rel_path in index[tok]:
                index[tok].remove(rel_path)
                if not index[tok]:
                    del index[tok]
        self._save()

    def candidates(self, tokens: list[str]) -> list[str]:
        """Relative paths whose docs contain any query token; [] if none."""
        index = self._load()
        rels: set[str] = set()
        for tok in tokens:
            rels.update(index.get(tok, []))
        return sorted(rels)

    def rebuild(self, memories: list[tuple[Memory, str]]) -> dict[str, int]:
        """Rebuild from (memory, rel_path) pairs; returns counts."""
        index: dict[str, list[str]] = {}
        for mem, rel in memories:
            for tok in set(tokenize(doc_text(mem))):
                index.setdefault(tok, []).append(rel)
        self._data = index
        self._save()
        return {"memories": len(memories), "tokens": len(index)}
