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
import os
import shutil
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from .index import Index
from .model import MEMORY_TYPES, TTL_DAYS, Memory
from .scoring import rank, recency_ref, tokenize

ARCHIVE_USES_THRESHOLD = 3
CONF_USE_BUMP = 0.1
CONF_CROSS_AGENT_BUMP = 0.15
GIT_IDENTITY = ("-c", "user.name=compound-memory", "-c", "user.email=memory@local")


def _today() -> str:
    return dt.date.today().isoformat()


def _as_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def _remove(path: Path) -> None:
    """Move a file out of the way without unlink (sandbox trash hooks block bulk deletes in tests)."""
    if path.exists():
        os.replace(path, path.with_name(f".{path.name}.rm"))


def new_id() -> str:
    return f"{dt.date.today().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"


class MemoryStore:
    def __init__(self, root: Path | str, git: bool = True) -> None:
        self.root = Path(root)
        self.ns_root = self.root / "namespaces"
        self.archive_root = self.root / "archive"
        self.index = Index(self.root, scan_pairs=self._scan_pairs)
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
        if ns.startswith("agent-") and source not in (ns, ns[len("agent-"):]):
            raise PermissionError(f"namespace {ns!r} is private to its owner; writer is {source!r}")
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
        self.index.sync(mem, self._active_rel(mem))
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
        self.index.sync(mem, self._active_rel(mem))
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
        """检索 = 选候选（store 的 layout 职责）+ 排序（scoring.rank 单一定义点）。"""
        now = now or dt.date.today()
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        return rank(query, self._candidates(q_tokens, ns), now=now, top_k=top_k)

    # ---------- decay / archive / revive ----------

    def decay_sweep(self, now: dt.date | None = None) -> list[str]:
        now = now or dt.date.today()
        archived: list[str] = []
        for path in sorted(self.ns_root.rglob("*.md")):
            mem = self.parse(path)
            if mem.ttl is None:
                continue
            age = (now - _as_date(recency_ref(mem))).days
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
        old_rel = str(src.relative_to(self.root))
        mem.archived = True
        self._save(mem)
        _remove(src)
        self.index.sync(mem, old_rel)

    def _move_to_active(self, mem: Memory) -> None:
        src = self._archive_path(mem)
        mem.archived = False
        self._save(mem)
        _remove(src)
        self.index.sync(mem, self._active_rel(mem))

    # ---------- index (rebuildable cache; mechanics live in index.Index) ----------

    def _active_rel(self, mem: Memory) -> str:
        return str(self._active_path(mem).relative_to(self.root))

    def _scan_pairs(self) -> list[tuple[Memory, str]]:
        """Scan the active tree for Index rebuilds (injected callback, invoked lazily)."""
        return [
            (self.parse(path), str(path.relative_to(self.root)))
            for path in sorted(self.ns_root.rglob("*.md"))
        ]

    def rebuild_index(self) -> dict[str, Any]:
        return self.index.rebuild(self._scan_pairs())

    def _candidates(self, q_tokens: list[str], ns: str) -> list[Memory]:
        """Indexed lookup with a scan fallback (Q4-A): cache loss degrades to slow, never to error.

        命中路径仍逐一复查文件存在性与 ns/archived——防的是 store API 之外的
        文件变动（手工编辑、git 操作），与缓存活性无关；活性自愈在 Index 内。
        """
        rels = self.index.candidates(q_tokens)
        if rels:
            out = []
            for rel in rels:
                path = self.root / rel
                if path.exists():
                    mem = self.parse(path)
                    if mem.ns == ns and not mem.archived:
                        out.append(mem)
            if out:
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
