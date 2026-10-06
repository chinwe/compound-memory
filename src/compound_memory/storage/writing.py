"""写路径动词（动词件，#38）：write / write_new（落库核心）/ write_result。

write_new 是四方共用核心（write 单条 / batch 批式（逐条照走 write）/
distill_apply 落产物 / tests 播种外部 ns fixture），本模块收 facade 侧的
写路径编排。门禁执行时序不动（ADR 0003 裁决 5）：write 是参数型动词，
key/有效期校验在 write 入口、ns 校验与私有 ns 越权拒绝在落库核心
write_new 开头（锁内）。_write_lock 的「文件写出 + 缓存更新 + commit」
临界区逐位保持。回跳 facade 的成员（_write_new/_write_result/_save/
_sync_indexes 等）一律经 store 实例属性查找（打桩缝，同 distill 的
batch/write 约束，#36 沉淀）。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import asdict
from typing import Any, Protocol, overload

from ..model import MEMORY_TYPES, TTL_DAYS, Memory
from ..review_queue import ReviewQueue
from .validation import check_key, check_project, check_validity


class WritingDeps(Protocol):
    """write 动词实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。

    _resolve_identity 镜像 facade 的 overload 对（mypy 结构匹配要求同构，
    同 ReviewDeps 的理由）；_write_new/_write_result 镜像 facade 薄委托签名
    ——write 经 store 属性查找回跳，与 tests 直呼 store._write_new 同缝。
    _write_result 在 facade 是 staticmethod，经实例属性查找调用（同
    SearchDeps.parse 的形态）。
    """

    _review_queue: ReviewQueue

    def today(self) -> str: ...
    def _new_id(self) -> str: ...
    def _check_ns(self, ns: str) -> None: ...
    def _find_by_key(self, ns: str, mtype: str, key: str, exclude_content: str) -> Memory | None: ...
    def _save(self, mem: Memory) -> None: ...
    def _sync_indexes(self, mem: Memory, rel_path: str) -> None: ...
    def _active_rel(self, mem: Memory) -> str: ...
    def _commit(self, message: str) -> None: ...
    def _write_lock(self) -> AbstractContextManager[None]: ...
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
        project: str | None = None,
    ) -> tuple[Memory, Memory | None]: ...
    def _write_result(self, mem: Memory, conflict_with: Memory | None) -> dict[str, Any]: ...

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...
    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...
    def _resolve_identity(self, value: str | None, role: str) -> str | None: ...


def write(
    store: WritingDeps,
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
    project: str | None = None,
) -> dict[str, Any]:
    """写路径正门：身份裁决 → 有效期校验 → 锁内落库（write_new）+ commit。"""
    source = store._resolve_identity(source, "source")
    check_validity(valid_from, valid_until)
    with store._write_lock():
        mem, conflict_with = store._write_new(
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
            project=project,
        )
        store._commit(f"write {mem.id} ({type}/{ns}) by {source}")
    return store._write_result(mem, conflict_with)


def write_new(
    store: WritingDeps,
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
    project: str | None = None,
) -> tuple[Memory, Memory | None]:
    """write 的落库核心（无 commit）：commit 由调用方动词收口——单条走 write，
    批式经 batch()（_commit 单点拦截）。tests 亦用它播种 write 会正当拒绝的
    外部 ns fixture（显式字段落库的测试种子）。"""
    if type not in MEMORY_TYPES:
        raise ValueError(f"type must be one of {MEMORY_TYPES}, got: {type!r}")
    check_key(key)
    check_project(project)  # ADR 0010：project slug 校验与 key 同点（write/batch/tests 共用）
    store._check_ns(ns)
    if ns.startswith("agent-") and source not in (ns, ns[len("agent-"):]):
        raise PermissionError(f"namespace {ns!r} is private to its owner; writer is {source!r}")
    conflict_with: Memory | None = None
    if key and type in ("fact", "insight"):
        conflict_with = store._find_by_key(ns, type, key, exclude_content=content)
    mem = Memory(
        id=store._new_id(),
        ns=ns,
        type=type,
        source=source,
        created=created or store.today(),
        content=content,
        confidence=0.5 if confidence is None else confidence,
        links=list(links or []),
        ttl=TTL_DAYS[type],
        key=key,
        origin=origin,
        valid_from=valid_from,
        valid_until=valid_until,
        project=project,
    )
    store._save(mem)
    if conflict_with is not None:
        store._review_queue.append(conflict_with, mem)
    store._sync_indexes(mem, store._active_rel(mem))
    return mem, conflict_with


def write_result(mem: Memory, conflict_with: Memory | None) -> dict[str, Any]:
    result = asdict(mem)
    result["conflict"] = conflict_with is not None
    if conflict_with is not None:
        result["conflicts_with"] = conflict_with.id
    return result
