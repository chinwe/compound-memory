"""存储层：Markdown + YAML frontmatter、命名空间、git、复利引擎。

根目录布局：
    namespaces/<ns>/<type>/<id>.md   活动记忆
    archive/<ns>/<type>/<id>.md      衰减归档（可恢复）
    index/tokens.json                可重建的检索缓存
    review-queue.md                  fact/insight 冲突队列
"""

from __future__ import annotations

import datetime as dt
import dataclasses
import shutil
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import yaml

from .index import Index
from .model import MEMORY_TYPES, TTL_DAYS, Memory
from .scoring import bm25_scores, doc_text, normalized_similarity, rank, recency_age, tokenize

ARCHIVE_USES_THRESHOLD = 3
CONF_USE_BUMP = 0.1
CONF_CROSS_AGENT_BUMP = 0.15
GIT_IDENTITY = ("-c", "user.name=compound-memory", "-c", "user.email=memory@local")
# 蒸馏信号阈值（distill-plan 单一定义点；--help 同步注明）：
# 疑似重复 = normalized_similarity(BM25/n_query_tokens) 达到该值；晋升建议 = episode 高活性门槛
DISTILL_DUP_SIM_THRESHOLD = 0.5
PROMOTION_USES_THRESHOLD = 5
# stats 健康度桶（#8：固定边界保证跨期可比，恒输出全桶）——
# uses 桶按复利语义划线：0=死本金、3=归档存活线（ARCHIVE_USES_THRESHOLD）、10+=高价值；
# confidence 桶：<0.3 低信、0.3-0.6 写入默认带、0.6-0.8 已验证、0.8+ 高置信
USES_HISTOGRAM_BUCKETS = ("0", "1-2", "3-5", "6-9", "10+")
CONFIDENCE_HISTOGRAM_BUCKETS = ("<0.3", "0.3-0.6", "0.6-0.8", "0.8-1.0")
RECENT_WINDOW_DAYS = 7


def _unlink_file(path: Path) -> None:
    """默认删除 adapter（测试侧经 conftest 注入沙箱安全版本）。"""
    if path.exists():
        path.unlink()


def _uses_bucket(uses: int) -> str:
    if uses >= 10:
        return "10+"
    if uses >= 6:
        return "6-9"
    if uses >= 3:
        return "3-5"
    if uses >= 1:
        return "1-2"
    return "0"


def _conf_bucket(conf: float) -> str:
    if conf < 0.3:
        return "<0.3"
    if conf < 0.6:
        return "0.3-0.6"
    if conf < 0.8:
        return "0.6-0.8"
    return "0.8-1.0"


def _within_days(date_str: str, days: int, now: dt.date) -> bool:
    """ISO 日期落在 [now-days, now] 内；坏日期/未来日期一律 False（坏数据不冒充活性）。"""
    try:
        age = (now - dt.date.fromisoformat(date_str)).days
    except (ValueError, TypeError):
        return False
    return 0 <= age <= days


class MemoryStore:
    def __init__(
        self,
        root: Path | str,
        git: bool = True,
        clock: Callable[[], dt.date] = dt.date.today,
        remover: Callable[[Path], None] | None = None,
    ) -> None:
        self.root = Path(root)
        self.ns_root = self.root / "namespaces"
        self.archive_root = self.root / "archive"
        self.index = Index(self.root, scan_pairs=self._scan_pairs)
        self.review_queue_path = self.root / "review-queue.md"
        self.git_enabled = git and shutil.which("git") is not None
        self._clock = clock
        self._remover = remover or _unlink_file
        self._ensure_layout()
        if self.git_enabled:
            self._git("init", "-q", check=False)
            self._git("add", "-A", check=False)
            self._git("commit", "-qm", "init compound-memory store", check=False)

    def _today(self) -> str:
        return self._clock().isoformat()

    def _new_id(self) -> str:
        return f"{self._clock().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"

    # ---------- 布局 / git ----------

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

    # ---------- 文件 IO ----------

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

    # ---------- 公开接口 ----------
    #
    # 接口错误约定（单一定义，adapter 各翻译一次）：
    # - 调用方错误（参数非法 / 越权写命名空间 / 自链接）⇒ 抛 ValueError / PermissionError；
    # - 目标记忆不存在 ⇒ 正常返回 {"found": False}——所有按 id 的动词恒含 found 键。

    @staticmethod
    def _check_ns(ns: str) -> None:
        """ns 格式校验（write/search 共用）：非法 ns 是调用方错误，必须抛错——
        search 侧静默返回空结果会让 agent 误判"无相关记忆"。"""
        if ns != "_shared" and not ns.startswith("agent-"):
            raise ValueError("ns must be '_shared' or start with 'agent-'")

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
        origin: str | None = None,
    ) -> dict[str, Any]:
        mem, conflict_with = self._write_new(
            content,
            type=type,
            source=source,
            ns=ns,
            key=key,
            links=links,
            created=created,
            confidence=confidence,
            origin=origin,
        )
        self._commit(f"write {mem.id} ({type}/{ns}) by {source}")
        return self._write_result(mem, conflict_with)

    def _write_new(
        self,
        content: str,
        type: str,
        source: str,
        ns: str,
        key: str | None,
        links: list[str] | None,
        created: str | None,
        confidence: float | None,
        origin: str | None,
    ) -> tuple[Memory, Memory | None]:
        """write 的无 commit 核心——distill_apply 复用它把产物写入 + 源归档收进一次 commit。"""
        if type not in MEMORY_TYPES:
            raise ValueError(f"type must be one of {MEMORY_TYPES}, got: {type!r}")
        self._check_ns(ns)
        if ns.startswith("agent-") and source not in (ns, ns[len("agent-"):]):
            raise PermissionError(f"namespace {ns!r} is private to its owner; writer is {source!r}")
        conflict_with: Memory | None = None
        if key and type in ("fact", "insight"):
            conflict_with = self._find_by_key(ns, type, key, exclude_content=content)
        mem = Memory(
            id=self._new_id(),
            ns=ns,
            type=type,
            source=source,
            created=created or self._today(),
            content=content,
            confidence=0.5 if confidence is None else confidence,
            links=list(links or []),
            ttl=TTL_DAYS[type],
            key=key,
            origin=origin,
        )
        self._save(mem)
        if conflict_with is not None:
            self._append_review(conflict_with, mem)
        self.index.sync(mem, self._active_rel(mem))
        return mem, conflict_with

    @staticmethod
    def _write_result(mem: Memory, conflict_with: Memory | None) -> dict[str, Any]:
        result = MemoryStore._to_dict(mem)
        result["conflict"] = conflict_with is not None
        if conflict_with is not None:
            result["conflicts_with"] = conflict_with.id
        return result

    def get(self, mem_id: str, include_neighbors: bool = True) -> dict[str, Any]:
        mem = self.find(mem_id)
        if mem is None:
            return {"found": False}
        result = self._to_dict(mem)
        result["found"] = True
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
        mem.last_used = self._today()
        self._save(mem)
        self.index.sync(mem, self._active_rel(mem))
        self._commit(f"feedback {mem.id} by {agent}: uses={mem.uses} conf={mem.confidence}")
        result = self._to_dict(mem)
        result["found"] = True
        return result

    def link(self, id_a: str, id_b: str) -> dict[str, Any]:
        if id_a == id_b:
            raise ValueError("cannot link a memory to itself")
        mem_a, mem_b = self.find(id_a), self.find(id_b)
        missing = [mid for mid, m in ((id_a, mem_a), (id_b, mem_b)) if m is None]
        if missing:
            return {"found": False, "missing": missing}
        assert mem_a is not None and mem_b is not None
        if id_b not in mem_a.links:
            mem_a.links.append(id_b)
        if id_a not in mem_b.links:
            mem_b.links.append(id_a)
        self._save(mem_a)
        self._save(mem_b)
        self._commit(f"link {id_a} <-> {id_b}")
        return {"found": True, "a": id_a, "b": id_b, "links": mem_a.links}

    def search(
        self,
        query: str,
        ns: str = "_shared",
        top_k: int = 5,
        now: dt.date | None = None,
        include_neighbors: bool = True,
    ) -> list[dict[str, Any]]:
        """检索 = 选候选（store 的 layout 职责）+ 排序（scoring.rank 单一定义点）。"""
        self._check_ns(ns)
        now = now or self._clock()
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        return rank(
            query,
            self._candidates(q_tokens, ns),
            now=now,
            top_k=top_k,
            neighbor_lookup=self._active_neighbors if include_neighbors else None,
        )

    def _active_neighbors(self, mem_id: str) -> list[Memory]:
        """邻居召回的数据源：hit 的一度 links，归档邻居不召回（截断/上限/去环归 rank）。"""
        mem = self.find(mem_id)
        if mem is None:
            return []
        out: list[Memory] = []
        for link_id in mem.links:
            neighbor = self.find(link_id)
            if neighbor is not None and not neighbor.archived:
                out.append(neighbor)
        return out

    # ---------- 衰减 / 归档 / 复活 ----------

    def decay_sweep(self, now: dt.date | None = None) -> list[str]:
        now = now or self._clock()
        archived: list[str] = []
        for path in sorted(self.ns_root.rglob("*.md")):
            mem = self.parse(path)
            if mem.ttl is None:
                continue
            age = recency_age(mem, now)
            if age is None:
                continue  # 坏/缺日期：跳过该条而非崩掉整场扫描（宁可不归档，不因坏数据丢记忆）
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
        result = self._to_dict(mem)
        result["found"] = True
        return result

    def _archive(self, mem: Memory) -> None:
        src = self._active_path(mem)
        old_rel = str(src.relative_to(self.root))
        mem.archived = True
        self._save(mem)
        self._remover(src)
        self.index.sync(mem, old_rel)

    def _move_to_active(self, mem: Memory) -> None:
        src = self._archive_path(mem)
        mem.archived = False
        self._save(mem)
        self._remover(src)
        self.index.sync(mem, self._active_rel(mem))

    # ---------- 蒸馏（确定性段；判断/摘要交调用方 Agent，CONTEXT.md: Distillation） ----------

    def distill_plan(
        self,
        window_days: int = 30,
        min_uses: int = 1,
        min_confidence: float = 0.5,
        ns: str = "_shared",
    ) -> dict[str, Any]:
        """蒸馏候选扫描：窗口 + 活性门过滤，产出带信号标注的建议清单（只标注不合并）。

        三类信号：merge_with（同 ns 同 type 同 key，强信号）、possible_dup_of
        （BM25 normalized_similarity ≥ DISTILL_DUP_SIM_THRESHOLD，弱信号）、
        promotion_candidate（episode 高活性，晋升建议——判断后置，#6）。
        归档区不参与；坏日期记忆按宁缺勿滥跳过。
        """
        self._check_ns(ns)
        now = self._clock()
        cands: list[Memory] = []
        for path in sorted((self.ns_root / ns).rglob("*.md")):
            mem = self.parse(path)
            age = recency_age(mem, now)
            if age is None or age > window_days:
                continue
            if mem.uses < min_uses or mem.confidence < min_confidence:
                continue
            cands.append(mem)
        docs_tokens = [tokenize(doc_text(m)) for m in cands]
        # 每条候选的 tokens 当 query 在候选集上算 BM25——语料语义与 rank 的候选集一致
        sims = [
            [normalized_similarity(rel, len(qt)) for rel in bm25_scores(qt, docs_tokens)]
            for qt in docs_tokens
        ]
        by_key: dict[tuple[str, str], list[int]] = {}
        for i, mem in enumerate(cands):
            if mem.key:
                by_key.setdefault((mem.type, mem.key), []).append(i)
        candidates: list[dict[str, Any]] = []
        for i, mem in enumerate(cands):
            merge_with = (
                [cands[j].id for j in by_key[(mem.type, mem.key)] if j != i] if mem.key else []
            )
            candidates.append(
                {
                    "id": mem.id,
                    "type": mem.type,
                    "key": mem.key,
                    "uses": mem.uses,
                    "confidence": mem.confidence,
                    "created": mem.created,
                    "last_used": mem.last_used,
                    "content": mem.content,
                    "merge_with": merge_with,
                    "possible_dup_of": [
                        cands[j].id for j in range(len(cands)) if j != i and sims[i][j] >= DISTILL_DUP_SIM_THRESHOLD
                    ],
                    "promotion_candidate": mem.type == "episode" and mem.uses >= PROMOTION_USES_THRESHOLD,
                }
            )
        return {
            "window_days": window_days,
            "min_uses": min_uses,
            "min_confidence": min_confidence,
            "ns": ns,
            "candidates": candidates,
        }

    def distill_apply(
        self,
        content: str,
        type: str,
        source: str,
        source_ids: list[str],
        ns: str = "_shared",
        key: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        """蒸馏落库（原子）：产物写入（links 溯源到全部源、origin=distillation）+
        源批量归档，收进一次 commit。源任一不存在 ⇒ 整体不落库（found: False）。
        产物与现存 fact/insight 的 key 冲突走既有 review 队列机制，不特殊对待。
        """
        source_ids = list(dict.fromkeys(source_ids))  # 去重保序：重复源只归档一次
        sources = [self.find(mid) for mid in source_ids]
        missing = [mid for mid, mem in zip(source_ids, sources) if mem is None]
        if missing:
            return {"found": False, "missing": missing}
        mem, conflict_with = self._write_new(
            content,
            type=type,
            source=source,
            ns=ns,
            key=key,
            links=source_ids,
            created=None,
            confidence=confidence,
            origin="distillation",
        )
        archived: list[str] = []
        for src in sources:
            assert src is not None
            if not src.archived:
                self._archive(src)
            archived.append(src.id)
        self._commit(f"distill apply {mem.id} <- " + ", ".join(archived))
        result = self._write_result(mem, conflict_with)
        result["found"] = True
        result["archived_sources"] = archived
        return result

    # ---------- 索引（可重建缓存；机制在 index.Index） ----------

    def _active_rel(self, mem: Memory) -> str:
        return str(self._active_path(mem).relative_to(self.root))

    def _scan_pairs(self) -> list[tuple[Memory, str]]:
        """扫描活动区供 Index 全量重建（注入回调，惰性调用）。"""
        return [
            (self.parse(path), str(path.relative_to(self.root)))
            for path in sorted(self.ns_root.rglob("*.md"))
        ]

    def rebuild_index(self) -> dict[str, Any]:
        return self.index.rebuild(self._scan_pairs())

    def _candidates(self, q_tokens: list[str], ns: str) -> list[Memory]:
        """Indexed lookup: 索引活性（跨进程重载/带外重建）由 Index 在内部自愈，
        这里全信索引命中，只逐一复查文件存在性与 ns/archived——防的是索引
        词条与手编文件内容的漂移（改内容不改目录 mtime，那条路走显式 rebuild）。
        """
        out: list[Memory] = []
        for rel in self.index.candidates(q_tokens):
            path = self.root / rel
            if path.exists():
                mem = self.parse(path)
                if mem.ns == ns and not mem.archived:
                    out.append(mem)
        return out

    # ---------- 冲突 / 统计 ----------

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
            f"- {self._today()} conflict `{new.ns}/{new.type}/{new.key}`: "
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
        uses_hist = {bucket: 0 for bucket in USES_HISTOGRAM_BUCKETS}
        conf_hist = {bucket: 0 for bucket in CONFIDENCE_HISTOGRAM_BUCKETS}
        total, archived, conf_sum = 0, 0, 0.0
        recent_feedback, cross_validated = 0, 0
        distilled_total, distilled_recent = 0, 0
        now = self._clock()
        for base, is_archive in ((self.ns_root, False), (self.archive_root, True)):
            for path in base.rglob("*.md"):
                mem = self.parse(path)
                total += 1
                if is_archive:
                    archived += 1
                by_type[mem.type] = by_type.get(mem.type, 0) + 1
                by_ns[mem.ns] = by_ns.get(mem.ns, 0) + 1
                conf_sum += mem.confidence
                uses_hist[_uses_bucket(mem.uses)] += 1
                conf_hist[_conf_bucket(mem.confidence)] += 1
                if mem.last_used and _within_days(mem.last_used, RECENT_WINDOW_DAYS, now):
                    recent_feedback += 1
                if len(set(mem.validated_by)) >= 2:
                    cross_validated += 1
                if mem.origin == "distillation":
                    distilled_total += 1
                    if _within_days(mem.created, RECENT_WINDOW_DAYS, now):
                        distilled_recent += 1
        return {
            "total": total,
            "archived": archived,
            "active": total - archived,
            "avg_confidence": round(conf_sum / total, 3) if total else 0.0,
            "by_type": by_type,
            "by_ns": by_ns,
            "review_queue_entries": len(self.review_queue()),
            "uses_histogram": uses_hist,
            "confidence_histogram": conf_hist,
            "recent_feedback_7d": recent_feedback,
            "cross_validated": cross_validated,
            "distilled_total": distilled_total,
            "distilled_recent_7d": distilled_recent,
        }

    def git_log(self, limit: int = 5) -> list[str]:
        if not self.git_enabled:
            return []
        proc = self._git("log", "--oneline", f"-{limit}")
        return [line for line in proc.stdout.splitlines() if line.strip()]
