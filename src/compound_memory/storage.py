"""Storage layer: Markdown + YAML frontmatter, namespaces, git, compounding engine.

Layout under root:
    namespaces/<ns>/<type>/<id>.md   active memories
    archive/<ns>/<type>/<id>.md      decayed, recoverable
    index/tokens.json                rebuildable search cache
    review-queue.md                  fact/insight conflict queue
"""

from __future__ import annotations

import datetime as dt
import dataclasses
import json
import shutil
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .scoring import (
    TYPE_WEIGHT,
    bm25_scores,
    final_score,
    normalized_similarity,
    recency_score,
    tokenize,
)

MEMORY_TYPES = ("episode", "fact", "insight", "skill")
TTL_DAYS: dict[str, int | None] = {"episode": 90, "fact": None, "insight": 180, "skill": None}
ARCHIVE_USES_THRESHOLD = 3
CONF_USE_BUMP = 0.1
CONF_CROSS_AGENT_BUMP = 0.15
GIT_IDENTITY = ("-c", "user.name=compound-memory", "-c", "user.email=memory@local")


def _today() -> str:
    return dt.date.today().isoformat()


def new_id() -> str:
    return f"{dt.date.today().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"


@dataclass
class Memory:
    id: str
    ns: str
    type: str
    source: str
    created: str
    content: str
    confidence: float = 0.5
    uses: int = 0
    last_used: str | None = None
    links: list[str] = field(default_factory=list)
    ttl: int | None = None
    key: str | None = None
    validated_by: list[str] = field(default_factory=list)
    archived: bool = False


class MemoryStore:
    def __init__(self, root: Path | str, git: bool = True) -> None:
        self.root = Path(root)
        self.ns_root = self.root / "namespaces"
        self.archive_root = self.root / "archive"
        self.index_dir = self.root / "index"
        self.review_queue_path = self.root / "review-queue.md"
        self.git_enabled = git and shutil.which("git") is not None
        self._ensure_layout()
        if self.git_enabled:
            self._git("init", "-q", check=False)
            self._git("add", "-A", check=False)
            self._git("commit", "-qm", "init compound-memory store", check=False)

    # ---------- layout / git ----------

    def _ensure_layout(self) -> None:
        shared = self.ns_root / "_shared"
        for t in MEMORY_TYPES:
            (shared / t).mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        gitignore = self.root / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("index/\n", encoding="utf-8")

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *GIT_IDENTITY, *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def _commit(self, message: str) -> None:
        if not self.git_enabled:
            return
        self._git("add", "-A", check=False)
        self._git("commit", "-qm", message, check=False)

    # ---------- file IO ----------

    def _active_path(self, mem: Memory) -> Path:
        return self.ns_root / mem.ns / mem.type / f"{mem.id}.md"

    def _archive_path(self, mem: Memory) -> Path:
        return self.archive_root / mem.ns / mem.type / f"{mem.id}.md"

    def _save(self, mem: Memory) -> None:
        path = self._archive_path(mem) if mem.archived else self._active_path(mem)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {}
        for key, value in asdict(mem).items():
            if key == "content":
                continue
            if value is None or value == "" or value == []:
                continue
            if key in ("uses",) and value == 0:
                continue
            if key == "archived" and not value:
                continue
            meta[key] = value
        body = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n" + mem.content.strip() + "\n"
        path.write_text(body, encoding="utf-8")

    @staticmethod
    def parse(path: Path) -> Memory:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError(f"bad memory file (missing frontmatter): {path}")
        _, fm, body = text.split("---\n", 2)
        meta = yaml.safe_load(fm) or {}
        meta["content"] = body.strip()
        defaults = {
            f.name: f.default
            for f in dataclasses.fields(Memory)
            if f.default is not dataclasses.MISSING and f.name != "content"
        }
        defaults.pop("content", None)
        return Memory(**{**defaults, **{k: v for k, v in meta.items() if k in {f.name for f in dataclasses.fields(Memory)}}})

    def find(self, mem_id: str) -> Memory | None:
        for base in (self.ns_root, self.archive_root):
            for path in base.rglob(f"{mem_id}.md"):
                return self.parse(path)
        return None

    @staticmethod
    def _to_dict(mem: Memory) -> dict[str, Any]:
        return asdict(mem)

    # ---------- public API ----------

    def write(
        self,
        content: str,
        type: str,
        source: str,
        ns: str = "_shared",
        key: str | None = None,
        links: list[str] | None = None,
        created: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        if type not in MEMORY_TYPES:
            raise ValueError(f"type must be one of {MEMORY_TYPES}, got: {type!r}")
        if ns != "_shared" and not ns.startswith("agent-"):
            raise ValueError("ns must be '_shared' or start with 'agent-'")
        conflict_with: Memory | None = None
        if key and type in ("fact", "insight"):
            conflict_with = self._find_by_key(ns, type, key, exclude_content=content)
        mem = Memory(
            id=new_id(),
            ns=ns,
            type=type,
            source=source,
            created=created or _today(),
            content=content,
            confidence=0.5 if confidence is None else confidence,
            links=list(links or []),
            ttl=TTL_DAYS[type],
            key=key,
        )
        self._save(mem)
        if conflict_with is not None:
            self._append_review(conflict_with, mem)
        self._update_index_for(mem)
        self._commit(f"write {mem.id} ({type}/{ns}) by {source}")
        result = self._to_dict(mem)
        result["conflict"] = conflict_with is not None
        if conflict_with is not None:
            result["conflicts_with"] = conflict_with.id
        return result

    def get(self, mem_id: str, include_neighbors: bool = True) -> dict[str, Any]:
        mem = self.find(mem_id)
        if mem is None:
            return {"found": False}
        result = self._to_dict(mem)
        if include_neighbors and mem.links:
            neighbors = [self._to_dict(n) for n in (self.find(l) for l in mem.links) if n is not None]
            result["neighbors"] = neighbors
        return result

    def feedback(self, mem_id: str, agent: str) -> dict[str, Any]:
        mem = self.find(mem_id)
        if mem is None:
            return {"found": False}
        if mem.archived:
            self._move_to_active(mem)
        mem.uses += 1
        bump = CONF_USE_BUMP
        if agent not in mem.validated_by:
            if agent != mem.source:
                bump += CONF_CROSS_AGENT_BUMP
            mem.validated_by.append(agent)
        mem.confidence = round(min(1.0, mem.confidence + bump), 3)
        mem.last_used = _today()
        self._save(mem)
        self._update_index_for(mem)
        self._commit(f"feedback {mem.id} by {agent}: uses={mem.uses} conf={mem.confidence}")
        return self._to_dict(mem)

    def link(self, id_a: str, id_b: str) -> dict[str, Any]:
        if id_a == id_b:
            raise ValueError("cannot link a memory to itself")
        mem_a, mem_b = self.find(id_a), self.find(id_b)
        missing = [mid for mid, m in ((id_a, mem_a), (id_b, mem_b)) if m is None]
        if missing:
            return {"ok": False, "missing": missing}
        assert mem_a is not None and mem_b is not None
        if id_b not in mem_a.links:
            mem_a.links.append(id_b)
        if id_a not in mem_b.links:
            mem_b.links.append(id_a)
        self._save(mem_a)
        self._save(mem_b)
        self._commit(f"link {id_a} <-> {id_b}")
        return {"ok": True, "a": id_a, "b": id_b, "links": mem_a.links}

    def search(
        self,
        query: str,
        ns: str = "_shared",
        top_k: int = 5,
        now: dt.date | None = None,
    ) -> list[dict[str, Any]]:
        now = now or dt.date.today()
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        candidates = self._candidates(q_tokens, ns)
        docs = [tokenize(m.content + " " + (m.key or "")) for m in candidates]
        rels = bm25_scores(q_tokens, docs)
        hits: list[dict[str, Any]] = []
        for mem, rel in zip(candidates, rels):
            if rel <= 0:
                continue
            sim = normalized_similarity(rel, len(q_tokens))
            rec = recency_score(mem.last_used, mem.created, mem.type, now)
            score = final_score(sim, mem.confidence, rec, mem.type)
            hits.append(
                {
                    "id": mem.id,
                    "score": round(score, 4),
                    "similarity": round(sim, 4),
                    "confidence": mem.confidence,
                    "uses": mem.uses,
                    "type": mem.type,
                    "ns": mem.ns,
                    "source": mem.source,
                    "content": mem.content,
                }
            )
        hits.sort(key=lambda h: -h["score"])
        return hits[:top_k]

    # ---------- decay / archive / revive ----------

    def decay_sweep(self, now: dt.date | None = None) -> list[str]:
        now = now or dt.date.today()
        archived: list[str] = []
        for path in sorted(self.ns_root.rglob("*.md")):
            mem = self.parse(path)
            if mem.ttl is None:
                continue
            age = (now - dt.date.fromisoformat(mem.created)).days
            if age > mem.ttl and mem.uses < ARCHIVE_USES_THRESHOLD:
                self._archive(mem)
                archived.append(mem.id)
        if archived:
            self._commit("decay: archive " + ", ".join(archived))
        return archived

    def revive(self, mem_id: str) -> dict[str, Any]:
        mem = self.find(mem_id)
        if mem is None:
            return {"found": False}
        if mem.archived:
            self._move_to_active(mem)
            self._save(mem)
            self._commit(f"revive {mem_id}")
        return self._to_dict(mem)

    def _archive(self, mem: Memory) -> None:
        src = self._active_path(mem)
        mem.archived = True
        self._save(mem)
        src.unlink(missing_ok=True)
        self._update_index_for(mem)

    def _move_to_active(self, mem: Memory) -> None:
        src = self._archive_path(mem)
        mem.archived = False
        self._save(mem)
        src.unlink(missing_ok=True)
        self._update_index_for(mem)

    # ---------- index (rebuildable cache) ----------

    def _index_path(self) -> Path:
        return self.index_dir / "tokens.json"

    def _load_index(self) -> dict[str, list[str]] | None:
        path = self._index_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _save_index(self, index: dict[str, list[str]]) -> None:
        tmp = self._index_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(self._index_path())

    @staticmethod
    def _index_tokens(mem: Memory) -> set[str]:
        return set(tokenize(mem.content + " " + (mem.key or "")))

    def _update_index_for(self, mem: Memory) -> None:
        index = self._load_index()
        if index is None:
            return
        rel = str((self._archive_path(mem) if mem.archived else self._active_path(mem)).relative_to(self.root))
        for tok in list(index):
            if rel in index[tok]:
                index[tok].remove(rel)
                if not index[tok]:
                    del index[tok]
        for tok in self._index_tokens(mem):
            index.setdefault(tok, []).append(rel)
        self._save_index(index)

    def rebuild_index(self) -> dict[str, Any]:
        index: dict[str, list[str]] = {}
        count = 0
        for base in (self.ns_root, self.archive_root):
            for path in base.rglob("*.md"):
                mem = self.parse(path)
                count += 1
                rel = str(path.relative_to(self.root))
                for tok in self._index_tokens(mem):
                    index.setdefault(tok, []).append(rel)
        self._save_index(index)
        return {"memories": count, "tokens": len(index)}

    def _candidates(self, q_tokens: list[str], ns: str) -> list[Memory]:
        index = self._load_index()
        if index is not None:
            rels: set[str] = set()
            for tok in q_tokens:
                rels.update(index.get(tok, []))
            out = []
            for rel in sorted(rels):
                path = self.root / rel
                if path.exists():
                    mem = self.parse(path)
                    if mem.ns == ns:
                        out.append(mem)
            return out
        base = self.ns_root / ns
        if not base.exists():
            return []
        return [self.parse(p) for p in sorted(base.rglob("*.md"))]

    # ---------- conflicts / stats ----------

    def _find_by_key(self, ns: str, mtype: str, key: str, exclude_content: str) -> Memory | None:
        base = self.ns_root / ns / mtype
        if not base.exists():
            return None
        for path in sorted(base.rglob("*.md")):
            mem = self.parse(path)
            if mem.key == key and mem.content.strip() != exclude_content.strip():
                return mem
        return None

    def _append_review(self, old: Memory, new: Memory) -> None:
        line = (
            f"- {_today()} conflict `{new.ns}/{new.type}/{new.key}`: "
            f"{old.id} ({old.source}: {old.content[:40]}) vs {new.id} ({new.source}: {new.content[:40]})\n"
        )
        with self.review_queue_path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def review_queue(self) -> list[str]:
        if not self.review_queue_path.exists():
            return []
        return [
            line[2:].rstrip("\n")
            for line in self.review_queue_path.read_text(encoding="utf-8").splitlines()
            if line.startswith("- ")
        ]

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        by_ns: dict[str, int] = {}
        total, archived, conf_sum = 0, 0, 0.0
        for base, is_archive in ((self.ns_root, False), (self.archive_root, True)):
            for path in base.rglob("*.md"):
                mem = self.parse(path)
                total += 1
                if is_archive:
                    archived += 1
                by_type[mem.type] = by_type.get(mem.type, 0) + 1
                by_ns[mem.ns] = by_ns.get(mem.ns, 0) + 1
                conf_sum += mem.confidence
        return {
            "total": total,
            "archived": archived,
            "active": total - archived,
            "avg_confidence": round(conf_sum / total, 3) if total else 0.0,
            "by_type": by_type,
            "by_ns": by_ns,
            "review_queue_entries": len(self.review_queue()),
        }

    def git_log(self, limit: int = 5) -> list[str]:
        if not self.git_enabled:
            return []
        proc = self._git("log", "--oneline", f"-{limit}")
        return [line for line in proc.stdout.splitlines() if line.strip()]
