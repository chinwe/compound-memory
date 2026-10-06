"""生命周期动词（动词件，#38）：feedback / decay_sweep / revive / archive / move_to_active。

复利引擎写侧（feedback，ADR-0007 证据驱动置信度）+ 衰减归档/复活（decay_sweep/
revive）。阈值常量随宿主动词模块（ADR 0003 裁决 6）单一定义于此；包级公开面
收窄（#39）后不再 re-export，storage.lifecycle 是唯一导入路径（见 __init__
docstring）。锁语义逐位保持（红线自查）：feedback/revive 的「find → 门禁 → 改
→ save → sync → commit」整链在 _write_lock 内——find 在锁外时并发 feedback
同一记忆会读到同一快照、后写覆盖前者，uses/confidence 丢更新（2026-10-05
并发测试实证）；decay_sweep 的「扫描 + 归档 + 收尾 commit」是单临界区。门禁
执行时序不动（裁决 5）：按 id 动词的 owner 校验在锁内 find 之后（ns 只有
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

from ..model import Memory, EVIDENCE_RECENT_CAP, evidence_view
from ..review_queue import ReviewQueue
from ..scoring import recency_age

# 归档/置信度阈值（lifecycle 单一定义点，storage.lifecycle 是唯一导入路径）：
# ARCHIVE_USES_THRESHOLD 是衰减扫描的归档存活线（uses 达线免归档）；
# CONF_USE_BUMP 每次成功使用分、CONF_CROSS_AGENT_BUMP 新验证者跨宿主加成
# （ADR-0007 折算表：success +0.1，跨宿主首验 +0.15，validated_by 记忆×宿主去重）；
# CONF_FAILURE_PENALTY 负面不对称折算（failure −0.2，重复累计）、
# CONF_FLOOR 数值地板（保持可检索、可复活）；封顶 1.0 沿用。
ARCHIVE_USES_THRESHOLD = 3
CONF_USE_BUMP = 0.1
CONF_CROSS_AGENT_BUMP = 0.15
CONF_FAILURE_PENALTY = 0.2
CONF_FLOOR = 0.05

# feedback outcome 全集（ADR-0007）：未知值是调用方错误（ValueError）；
# success 为缺省——老调用方零破坏。
FEEDBACK_OUTCOMES = ("success", "failure", "contradiction", "obsolete", "unknown")


class LifecycleDeps(Protocol):
    """lifecycle 动词实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。

    _resolve_identity 镜像 facade 的 overload 对（同 ReviewDeps 的理由）；
    _archive/_move_to_active 镜像 facade 薄委托签名——feedback/revive/
    decay_sweep 经 store 属性查找回跳，与 review/distill 的 Deps 同缝。
    _review_queue 供 feedback 做冻结判定（ADR-0007：队列存在该记忆的未裁决
    contradiction 行 ⇒ 数值冻结）与行登记。
    """

    root: Path
    ns_root: Path
    _clock: Callable[[], dt.date]
    _remover: Callable[[Path], None]
    _review_queue: ReviewQueue

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


def fold_failure(mem: Memory) -> None:
    """failure 折算单点（feedback 与 review-resolve uphold 共用，无第二份拷贝）：
    conf −0.2 地板 0.05、failure_count+1（ADR-0007 折算表 + 裁决「维持」折算）。"""
    mem.confidence = round(max(CONF_FLOOR, mem.confidence - CONF_FAILURE_PENALTY), 3)
    ev = evidence_view(mem)
    ev["failure_count"] = ev.get("failure_count", 0) + 1
    mem.evidence = ev


def feedback(store: LifecycleDeps, mem_id: str, agent: str, outcome: str = "success") -> dict[str, Any]:
    """证据驱动置信度写侧（ADR-0007/0008）：outcome 折算表——success +0.1
    （跨宿主首验 +0.15，validated_by 记忆×宿主去重）、failure −0.2 重复累计
    （地板 0.05 封顶 1.0）、contradiction 数值冻结并登记队列独立行型、obsolete
    无条件立即归档（复活走既有 feedback 自动复活通道）、unknown 仅记事件。
    验证分（validated_by / 跨宿主加成）只属于 success——failure/contradiction
    是负证据或争议，不吃也不发验证分。冻结 = 队列存在该记忆的未裁决
    contradiction 行（ReviewQueue 单点派生，行清掉即解冻）；冻结期事件照记
    （uses/计数/明细/validated_by），数值原地。证据块绝不进 doc_text——
    feedback 内容不变零重编码（test_vector_index 行为钉）。"""
    if outcome not in FEEDBACK_OUTCOMES:
        raise ValueError(f"outcome must be one of {FEEDBACK_OUTCOMES}, got: {outcome!r}")
    agent = store._resolve_identity(agent, "agent")
    # 读-改-写全程临界区：find 在锁外时并发 feedback 同一记忆会读到同一
    # 快照、后写覆盖前者，uses/confidence 丢更新（2026-10-05 并发测试实证）
    with store._write_lock():
        mem = store.find(mem_id)
        if mem is None:
            return {"found": False}
        # 私有记忆只有属主可反馈：防外来 agent 刷 uses/confidence、混入 validated_by 或复活归档
        store._check_ns_owner(mem.ns, agent, role="agent")
        # 冻结判定在本次登记之前：本调用自己登记的行不该冻结自己的事件
        frozen = mem.id in store._review_queue.pending_contradictions()
        if outcome == "obsolete":
            # 无条件立即归档（ADR-0007：不引入「obsolete 但未归档」中间态）；
            # 已归档记忆不复活再归档空转，原位记账
            ev = evidence_view(mem)  # 惰性迁移先于事件：视图基于事件前的 uses
            mem.uses += 1
            mem.last_used = store.today()
            ev.setdefault("recent", []).append({"date": store.today(), "agent": agent, "outcome": outcome})
            ev["recent"] = ev["recent"][-EVIDENCE_RECENT_CAP:]
            mem.evidence = ev
            if not mem.archived:
                store._archive(mem)  # 含 save（归档区）/ 删活动区 / 索引收口
            else:
                store._save(mem)  # 原位更新 frontmatter 记账，索引无变化
            store._commit(f"feedback {mem.id} by {agent}: outcome={outcome} uses={mem.uses} conf={mem.confidence}")
        else:
            if mem.archived:
                store._move_to_active(mem)
            ev = evidence_view(mem)  # 惰性迁移先于事件：视图基于事件前的 uses（存量 uses → success_count）
            mem.uses += 1
            mem.last_used = store.today()
            ev.setdefault("recent", []).append({"date": store.today(), "agent": agent, "outcome": outcome})
            ev["recent"] = ev["recent"][-EVIDENCE_RECENT_CAP:]
            new_validator = False
            if outcome == "success":
                ev["success_count"] = ev.get("success_count", 0) + 1
                ev["last_verified"] = store.today()
                if agent not in mem.validated_by:
                    new_validator = agent != mem.source
                    mem.validated_by.append(agent)
            elif outcome == "failure":
                ev["failure_count"] = ev.get("failure_count", 0) + 1
            elif outcome == "contradiction":
                ev["contradiction_count"] = ev.get("contradiction_count", 0) + 1
                store._review_queue.append_contradiction(mem, agent)
            # obsolete/unknown 块内无计数槽位：仅明细 + 提交消息承载（全史走 git）
            mem.evidence = ev
            if not frozen:
                if outcome == "success":
                    bump = CONF_USE_BUMP + (CONF_CROSS_AGENT_BUMP if new_validator else 0)
                    mem.confidence = round(min(1.0, mem.confidence + bump), 3)
                elif outcome == "failure":
                    mem.confidence = round(max(CONF_FLOOR, mem.confidence - CONF_FAILURE_PENALTY), 3)
                # contradiction/unknown：数值不动（前者冻结语义本身，后者仅记账）
            store._save(mem)
            store._sync_indexes(mem, store._active_rel(mem))
            store._commit(f"feedback {mem.id} by {agent}: outcome={outcome} uses={mem.uses} conf={mem.confidence}")
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
