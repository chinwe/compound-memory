"""生命周期动词（动词件，#38）：feedback / decay_sweep / revive / archive / move_to_active。

复利引擎写侧（feedback）+ 衰减归档/复活（decay_sweep/revive）。阈值常量
随宿主动词模块（ADR 0003 裁决 6），__init__ re-export 保旧导入名。锁语义
逐位保持（红线自查）：feedback/revive 的「find → 门禁 → 改 → save → sync
→ commit」整链在 _write_lock 内——find 在锁外时并发 feedback 同一记忆会
读到同一快照、后写覆盖前者，uses/confidence 丢更新（2026-10-05 并发测试
实证）；decay_sweep 的「扫描 + 归档 + 收尾 commit」是单临界区。门禁执行
时序不动（裁决 5）：按 id 动词的 owner 校验在锁内 find 之后（ns 只有
find 后才知道）。回跳 facade 的成员（_archive/_move_to_active 等）一律经
store 实例属性查找——review/distill 的 Deps 与 tests 直呼 store._archive
走的是 facade 薄委托同一缝（打桩缝，#36 沉淀）。
"""

from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, overload

from ..model import Memory
from ..scoring import recency_age

# 归档/置信度阈值（lifecycle 单一定义点，__init__ re-export 保旧名）：
# ARCHIVE_USES_THRESHOLD 是衰减扫描的归档存活线（uses 达线免归档）；
# CONF_USE_BUMP 每次使用分、CONF_CROSS_AGENT_BUMP 新验证者跨 agent 加成
# （P1 公式：bump = 0.1/次 + 0.15 仅当「新验证者 ∧ ≠source」，契约 test_feedback 钉住）
ARCHIVE_USES_THRESHOLD = 3
CONF_USE_BUMP = 0.1
CONF_CROSS_AGENT_BUMP = 0.15


class LifecycleDeps(Protocol):
    """lifecycle 动词实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。

    _resolve_identity 镜像 facade 的 overload 对（同 ReviewDeps 的理由）；
    _archive/_move_to_active 镜像 facade 薄委托签名——feedback/revive/
    decay_sweep 经 store 属性查找回跳，与 review/distill 的 Deps 同缝。
    """

    root: Path
    ns_root: Path
    _clock: Callable[[], dt.date]
    _remover: Callable[[Path], None]

    def today(self) -> str: ...
    def _write_lock(self) -> AbstractContextManager[None]: ...
    def find(self, mem_id: str) -> Memory | None: ...
    def _check_ns_owner(self, ns: str, identity: str | None, role: str = "reader") -> None: ...
    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]: ...
    def _save(self, mem: Memory) -> None: ...
    def _sync_indexes(self, mem: Memory, rel_path: str) -> None: ...
    def _active_rel(self, mem: Memory) -> str: ...
    def _active_path(self, mem: Memory) -> Path: ...
    def _archive_path(self, mem: Memory) -> Path: ...
    def _archive(self, mem: Memory) -> None: ...
    def _move_to_active(self, mem: Memory) -> None: ...
    def _commit(self, message: str) -> None: ...

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...
    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...
    def _resolve_identity(self, value: str | None, role: str) -> str | None: ...


def feedback(store: LifecycleDeps, mem_id: str, agent: str) -> dict[str, Any]:
    agent = store._resolve_identity(agent, "agent")
    # 读-改-写全程临界区：find 在锁外时并发 feedback 同一记忆会读到同一
    # 快照、后写覆盖前者，uses/confidence 丢更新（2026-10-05 并发测试实证）
    with store._write_lock():
        mem = store.find(mem_id)
        if mem is None:
            return {"found": False}
        # 私有记忆只有属主可反馈：防外来 agent 刷 uses/confidence、混入 validated_by 或复活归档
        store._check_ns_owner(mem.ns, agent, role="agent")
        if mem.archived:
            store._move_to_active(mem)
        mem.uses += 1
        bump = CONF_USE_BUMP
        if agent not in mem.validated_by:
            if agent != mem.source:
                bump += CONF_CROSS_AGENT_BUMP
            mem.validated_by.append(agent)
        mem.confidence = round(min(1.0, mem.confidence + bump), 3)
        mem.last_used = store.today()
        store._save(mem)
        store._sync_indexes(mem, store._active_rel(mem))
        store._commit(f"feedback {mem.id} by {agent}: uses={mem.uses} conf={mem.confidence}")
    result = asdict(mem)
    result["found"] = True
    return result


def decay_sweep(store: LifecycleDeps) -> list[str]:
    now = store._clock()
    with store._write_lock():  # 批量归档 + 收尾 commit 一个临界区
        archived: list[str] = []
        for mem, _path in store._scan_parsed(store.ns_root):
            if mem.ttl is None:
                continue
            age = recency_age(mem, now)
            if age is None:
                continue  # 坏/缺日期：跳过该条而非崩掉整场扫描（宁可不归档，不因坏数据丢记忆）
            if age > mem.ttl and mem.uses < ARCHIVE_USES_THRESHOLD:
                store._archive(mem)
                archived.append(mem.id)
        if archived:
            store._commit("decay: archive " + ", ".join(archived))
    return archived


def revive(store: LifecycleDeps, mem_id: str, reader: str | None = None) -> dict[str, Any]:
    reader = store._resolve_identity(reader, "reader")
    with store._write_lock():  # 读-改-写全程临界区（同 feedback 的丢更新防御）
        mem = store.find(mem_id)
        if mem is None:
            return {"found": False}
        # revive 返回全文，与 get 同属按 id 读路径：私有 ns 仅属主可复活
        store._check_ns_owner(mem.ns, reader)
        if mem.archived:
            store._move_to_active(mem)
            store._save(mem)
            store._commit(f"revive {mem_id}")
    result = asdict(mem)
    result["found"] = True
    return result


def archive(store: LifecycleDeps, mem: Memory) -> None:
    src = store._active_path(mem)
    old_rel = src.relative_to(store.root).as_posix()
    mem.archived = True
    store._save(mem)
    store._remover(src)
    store._sync_indexes(mem, old_rel)


def move_to_active(store: LifecycleDeps, mem: Memory) -> None:
    src = store._archive_path(mem)
    mem.archived = False
    store._save(mem)
    store._remover(src)
    store._sync_indexes(mem, store._active_rel(mem))
