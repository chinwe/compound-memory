"""MemoryStore 组合点（facade，ADR 0003 / #36）：机制件装配 + 全部动词方法。

骨架片：storage.py 原样迁入本包，模块代码零改动；后续机制件（paths/files/
gitlayer/locking/validation）逐片外移后，facade 对应方法退化为薄委托；
动词方法体的外移归动词票（#37/#38）。包级布局与旧导入面见 __init__.py。
"""

from __future__ import annotations

import datetime as dt
import subprocess
import uuid
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator, overload

from ..index import Index
from ..model import MEMORY_TYPES, TTL_DAYS, Memory
from ..review_queue import ReviewQueue
from ..scoring import recency_age
from ..vector_index import VectorIndex
from . import distill, files, gitlayer, indexing, locking, paths, review, search as search_mod, stats as stats_mod, validation
from .files import _unlink_file
from .gitlayer import _git_available
from .locking import _Batch
from .validation import _PATH_COMPONENT_RE, check_key, check_validity as _check_validity

ARCHIVE_USES_THRESHOLD = 3
CONF_USE_BUMP = 0.1
CONF_CROSS_AGENT_BUMP = 0.15


class MemoryStore:
    def __init__(
        self,
        root: Path | str,
        git: bool = True,
        clock: Callable[[], dt.date] = dt.date.today,
        remover: Callable[[Path], None] | None = None,
        git_probe: Callable[[], bool] | None = None,
        embedder: Callable[[list[str]], list[list[float]]] | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.ns_root = paths.ns_root(self.root)
        self.archive_root = paths.archive_root(self.root)
        self.index = Index(self.root, scan_pairs=self._scan_pairs)
        self.vector_index = VectorIndex(self.root, scan_pairs=self._scan_pairs, embedder=embedder)
        self._embedder = embedder
        self._review_queue = ReviewQueue(self.root / "review-queue.md", clock=clock)
        self.git_enabled = git and (git_probe or _git_available)()
        # git 仓库发现的天花板（防逃逸）语义见 gitlayer.git_env
        self._git_env = gitlayer.git_env(self.root)
        self._clock = clock
        self._remover = remover or _unlink_file
        # 进程侧身份证明：agent_id 非空时（宿主经 COMPOUND_MEMORY_AGENT_ID 注入），
        # 所有调用方自报身份（source/reader/agent）必须与其一致，缺省 reader 自动补真值。
        # 只由 server/cli 入口显式传入，store 自身不读环境变量（测试与库调用保持确定性）。
        self.agent_id = agent_id
        self._locking = locking.WriteLocker(self.root)  # 写锁 + batch 协调状态单点
        self._ensure_layout()
        if self.git_enabled and not (self.root / ".git").exists():
            # init commit 仅限首次创建：__init__ 在每次 CLI/MCP 启动都会执行，
            # 无条件 add -A + commit 会把带外手编的文件吞进误导性的 "init" 提交
            gitlayer.init_commit(self._git)
        elif self.git_enabled:
            self._recover_orphan_changes()

    def today(self) -> str:
        """当前日期（ISO，注入 clock 的公开出口）：created 缺省、抽取清单等消费。"""
        return self._clock().isoformat()

    def _new_id(self) -> str:
        return f"{self._clock().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"

    # ---------- 批式落库通道（协调体归机制件 locking） ----------

    @contextmanager
    def batch(self, message: str | None = None) -> Iterator[_Batch]:
        """批量落库的正门：逐条校验照走、commit 与索引落盘收拢批尾（灌库/蒸馏用）。

        - 每条 write 照常逐条校验并立即落盘（写穿），校验语义与单条 write 完全一致；
          变化只在提交粒度：批尾一次索引 flush（词法落盘一次、向量一次性批量编码）
          + 一次 git commit。逐条 write 的每条全量重写 tokens.json 是 O(n²) 的来源。
        - 失败语义「落地即已提交」：批内异常时已写入条目照常 flush + commit
          （消息注明 partial）后原样上抛——不存在静默半提交。
        - 批内可见性无承诺：同进程词面读经内存索引可能看到已写入条目，向量路与
          跨进程读要等批尾 flush——需要一致快照的调用方不要在批内检索。
        - 嵌套 batch 是调用方错误（ValueError）；feedback/link 等动词的 commit
          在批内同样延迟（_commit 单点拦截），各动词无需批式特化版本。
        """
        with self._locking.batch(message, self.index, self.vector_index, self._commit) as handle:
            yield handle

    # ---------- 跨进程写锁（机制件 locking 的薄委托） ----------

    def _write_lock(self) -> AbstractContextManager[None]:
        return self._locking.write_lock()

    @property
    def _write_lock_depth(self) -> int:
        # 测试缝保留：锁重入深度的可观测出口（git_durability 的 TOCTOU 探针读它）
        return self._locking.lock_depth

    # ---------- 布局 / git（机制件 paths/gitlayer 的薄委托） ----------

    def _ensure_layout(self) -> None:
        paths.ensure_layout(self.root)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return gitlayer.run(self.root, self._git_env, args, check=check)

    def _git_retry(self, *args: str) -> subprocess.CompletedProcess[str]:
        return gitlayer.retry(self._git, *args)

    def _recover_orphan_changes(self) -> None:
        gitlayer.recover_orphan_changes(self._git, self._write_lock, self._commit)

    def _commit(self, message: str) -> None:
        gitlayer.commit(self._git, message, enabled=self.git_enabled, defer=self._locking.defer_commit)

    # ---------- 文件 IO（机制件 files/paths 的薄委托） ----------

    def _active_path(self, mem: Memory) -> Path:
        return paths.active_path(self.root, mem)

    def _archive_path(self, mem: Memory) -> Path:
        return paths.archive_path(self.root, mem)

    def _save(self, mem: Memory) -> None:
        files.save(mem, self.root)

    @staticmethod
    def parse(path: Path) -> Memory:
        return files.parse(path)

    def _parse_for_scan(self, path: Path) -> Memory | None:
        return files.parse_for_scan(path)

    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]:
        return files.scan_parsed(base)

    def find(self, mem_id: str) -> Memory | None:
        return files.find(mem_id, self.root)

    # ---------- 公开接口 ----------
    #
    # 接口错误约定（单一定义，adapter 各翻译一次）：
    # - 调用方错误（参数非法 / 越权写命名空间 / 自链接）⇒ 抛 ValueError / PermissionError；
    # - 目标记忆不存在 ⇒ 正常返回 {"found": False}——所有按 id 的动词恒含 found 键。

    @staticmethod
    def _check_ns(ns: str) -> None:
        """门禁薄委托：定义单点在 validation.check_ns（执行时序不动）。"""
        validation.check_ns(ns)

    @staticmethod
    def _check_ns_owner(ns: str, identity: str | None, role: str = "reader") -> None:
        """门禁薄委托：定义单点在 validation.check_ns_owner（执行时序不动）。"""
        validation.check_ns_owner(ns, identity, role)

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...

    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...

    def _resolve_identity(self, value: str | None, role: str) -> str | None:
        """身份裁决薄委托：定义单点在 validation.resolve_identity（self.agent_id 注入）。"""
        return validation.resolve_identity(value, role, self.agent_id)

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
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> dict[str, Any]:
        source = self._resolve_identity(source, "source")
        _check_validity(valid_from, valid_until)
        with self._write_lock():
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
                valid_from=valid_from,
                valid_until=valid_until,
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
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> tuple[Memory, Memory | None]:
        """write 的落库核心（无 commit）：commit 由调用方动词收口——单条走 write，
        批式经 batch()（_commit 单点拦截）。tests 亦用它播种 write 会正当拒绝的
        外部 ns fixture（显式字段落库的测试种子）。"""
        if type not in MEMORY_TYPES:
            raise ValueError(f"type must be one of {MEMORY_TYPES}, got: {type!r}")
        check_key(key)
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
            created=created or self.today(),
            content=content,
            confidence=0.5 if confidence is None else confidence,
            links=list(links or []),
            ttl=TTL_DAYS[type],
            key=key,
            origin=origin,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        self._save(mem)
        if conflict_with is not None:
            self._review_queue.append(conflict_with, mem)
        self._sync_indexes(mem, self._active_rel(mem))
        return mem, conflict_with

    @staticmethod
    def _write_result(mem: Memory, conflict_with: Memory | None) -> dict[str, Any]:
        result = asdict(mem)
        result["conflict"] = conflict_with is not None
        if conflict_with is not None:
            result["conflicts_with"] = conflict_with.id
        return result

    def get(self, mem_id: str, include_neighbors: bool = True, reader: str | None = None) -> dict[str, Any]:
        reader = self._resolve_identity(reader, "reader")
        mem = self.find(mem_id)
        if mem is None:
            return {"found": False}
        self._check_ns_owner(mem.ns, reader)
        result = asdict(mem)
        result["found"] = True
        # links 输出同 ns 脱敏：跨 ns 遗留链不把对侧 id 暴露给本侧读者（与邻居召回同规则）
        same_ns_links: list[str] = []
        for l in mem.links:
            t = self.find(l)
            if t is None or t.ns == mem.ns:
                same_ns_links.append(l)
        result["links"] = same_ns_links
        if include_neighbors and mem.links:
            neighbors = [asdict(n) for n in (self.find(l) for l in same_ns_links) if n is not None]
            result["neighbors"] = neighbors
        return result

    def feedback(self, mem_id: str, agent: str) -> dict[str, Any]:
        agent = self._resolve_identity(agent, "agent")
        # 读-改-写全程临界区：find 在锁外时并发 feedback 同一记忆会读到同一
        # 快照、后写覆盖前者，uses/confidence 丢更新（2026-10-05 并发测试实证）
        with self._write_lock():
            mem = self.find(mem_id)
            if mem is None:
                return {"found": False}
            # 私有记忆只有属主可反馈：防外来 agent 刷 uses/confidence、混入 validated_by 或复活归档
            self._check_ns_owner(mem.ns, agent, role="agent")
            if mem.archived:
                self._move_to_active(mem)
            mem.uses += 1
            bump = CONF_USE_BUMP
            if agent not in mem.validated_by:
                if agent != mem.source:
                    bump += CONF_CROSS_AGENT_BUMP
                mem.validated_by.append(agent)
            mem.confidence = round(min(1.0, mem.confidence + bump), 3)
            mem.last_used = self.today()
            self._save(mem)
            self._sync_indexes(mem, self._active_rel(mem))
            self._commit(f"feedback {mem.id} by {agent}: uses={mem.uses} conf={mem.confidence}")
        result = asdict(mem)
        result["found"] = True
        return result

    def link(self, id_a: str, id_b: str, agent: str | None = None) -> dict[str, Any]:
        """双向关联两条记忆（复利来源②）。跨 ns 禁止（ValueError）；同 ns 私有记忆
        仅属主可连（agent 参数，与 feedback 同规——D1/#34：link 是最后一个无身份
        写入口，按 id 动词在锁内 find 后过门）；_shared 无需身份。"""
        agent = self._resolve_identity(agent, "agent")
        if id_a == id_b:
            raise ValueError("cannot link a memory to itself")
        with self._write_lock():  # 读-改-写全程临界区（同 feedback 的丢更新防御）
            mem_a, mem_b = self.find(id_a), self.find(id_b)
            missing = [mid for mid, m in ((id_a, mem_a), (id_b, mem_b)) if m is None]
            if missing:
                return {"found": False, "missing": missing}
            assert mem_a is not None and mem_b is not None
            # 跨 ns 链会把对侧 id 写进本侧文件 frontmatter，成为私有 id 的泄漏源；
            # 且邻居召回本就同 ns 过滤，跨 ns 链对复利无贡献——创建侧直接禁止
            if mem_a.ns != mem_b.ns:
                raise ValueError(f"cannot link memories across namespaces: {mem_a.ns!r} vs {mem_b.ns!r}")
            # D1 属主门禁：跨 ns 已在上面拒绝，此处两侧同 ns——私有 ns 的 link
            # 写入 frontmatter 同属私有数据变更，仅属主可做（fail-closed，缺身份即拒）
            self._check_ns_owner(mem_a.ns, agent, role="agent")
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
        ns: str | None = None,
        top_k: int = 5,
        include_neighbors: bool = True,
        reader: str | None = None,
    ) -> list[dict[str, Any]]:
        """检索动词转发：实现体在动词件 search.py（门禁时序/结果形状原样）。"""
        return search_mod.search(
            self, query, ns=ns, top_k=top_k, include_neighbors=include_neighbors, reader=reader
        )

    # ---------- 衰减 / 归档 / 复活 ----------

    def decay_sweep(self) -> list[str]:
        now = self._clock()
        with self._write_lock():  # 批量归档 + 收尾 commit 一个临界区
            archived: list[str] = []
            for mem, _path in self._scan_parsed(self.ns_root):
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

    def revive(self, mem_id: str, reader: str | None = None) -> dict[str, Any]:
        reader = self._resolve_identity(reader, "reader")
        with self._write_lock():  # 读-改-写全程临界区（同 feedback 的丢更新防御）
            mem = self.find(mem_id)
            if mem is None:
                return {"found": False}
            # revive 返回全文，与 get 同属按 id 读路径：私有 ns 仅属主可复活
            self._check_ns_owner(mem.ns, reader)
            if mem.archived:
                self._move_to_active(mem)
                self._save(mem)
                self._commit(f"revive {mem_id}")
        result = asdict(mem)
        result["found"] = True
        return result

    def _archive(self, mem: Memory) -> None:
        src = self._active_path(mem)
        old_rel = src.relative_to(self.root).as_posix()
        mem.archived = True
        self._save(mem)
        self._remover(src)
        self._sync_indexes(mem, old_rel)

    def _move_to_active(self, mem: Memory) -> None:
        src = self._archive_path(mem)
        mem.archived = False
        self._save(mem)
        self._remover(src)
        self._sync_indexes(mem, self._active_rel(mem))

    # ---------- 蒸馏（确定性段；判断/摘要交调用方 Agent，CONTEXT.md: Distillation） ----------

    def distill_plan(
        self,
        window_days: int = 30,
        min_uses: int = 1,
        min_confidence: float = 0.5,
        ns: str = "_shared",
        reader: str | None = None,
    ) -> dict[str, Any]:
        """蒸馏动词转发：实现体与阈值常量在动词件 distill.py。"""
        return distill.distill_plan(
            self, window_days=window_days, min_uses=min_uses, min_confidence=min_confidence, ns=ns, reader=reader
        )

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
        """蒸馏动词转发：实现体在动词件 distill.py（batch/write 经属性查找）。"""
        return distill.distill_apply(
            self, content, type=type, source=source, source_ids=source_ids, ns=ns, key=key, confidence=confidence
        )

    # ---------- 索引（可重建缓存；机制在 index.Index 与 vector_index.VectorIndex） ----------

    def _active_rel(self, mem: Memory) -> str:
        return paths.active_rel(self.root, mem)

    def _sync_indexes(self, mem: Memory, rel_path: str) -> None:
        """索引收口薄委托：实现体在动词件 indexing.py（写路径全部经此收口）。"""
        indexing.sync_indexes(self, mem, rel_path)

    def _scan_pairs(self) -> list[tuple[Memory, str]]:
        """索引扫描薄委托：实现体在动词件 indexing.py（构造注入回调 + tests 触达面）。"""
        return indexing.scan_pairs(self)

    def rebuild_index(self) -> dict[str, Any]:
        """索引动词转发：实现体在动词件 indexing.py。"""
        return indexing.rebuild_index(self)

    def lexical_candidates(
        self, q_tokens: list[str], nss: set[str], reader: str | None = None
    ) -> list[Memory]:
        """检索动词转发：实现体在动词件 search.py（公开词面候选正门）。"""
        return search_mod.lexical_candidates(self, q_tokens, nss, reader=reader)

    # ---------- 冲突 / 统计 ----------

    def _find_by_key(self, ns: str, mtype: str, key: str, exclude_content: str) -> Memory | None:
        """key 冲突域查找薄委托：实现体在动词件 review.py（_write_new 在用）。"""
        return review.find_by_key(self, ns, mtype, key, exclude_content)

    def review_queue(self) -> list[str]:
        """评审队列动词转发：实现体在动词件 review.py。"""
        return review.review_queue(self)

    def review_resolve(
        self, ids: list[str] | None = None, all: bool = False, reader: str | None = None
    ) -> dict[str, Any]:
        """评审队列动词转发：实现体在动词件 review.py（D2 门禁与临界区语义原样）。"""
        return review.review_resolve(self, ids=ids, all=all, reader=reader)

    def stats(self) -> dict[str, Any]:
        """统计动词转发：实现体与桶函数/桶常量在动词件 stats.py。"""
        return stats_mod.stats(self)

    def git_log(self, limit: int = 5) -> list[str]:
        return gitlayer.log_lines(self._git, limit, self.git_enabled)
