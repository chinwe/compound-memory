"""离线编排动词（CLI-only，#69）：dream = distill_plan + 衰减信号 + stats 快照。

ADR-0001：确定性准备，判断段留在调用方 Agent。本模块只编排既有读路径，
默认（也是唯一）路径是空跑——不归档、不 distill_apply、不 forget、不写
feedback / evidence，不进 MCP（恰好 5 tool 红线）。无 LLM。

衰减计数复用 lifecycle.is_decay_candidate（与 decay_sweep 同一谓词），
不另开写路径。valid_until 已过走 scoring.is_expired，与 stats.expired_active
同一日期规则，范围收成单个 ns 的活动区。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, overload

from ..model import Memory
from ..scoring import is_expired
from .lifecycle import is_decay_candidate


class DreamDeps(Protocol):
    """dream 实际触碰的 store 面（窄 Deps）。

    回跳 distill_plan / stats 经 store 实例属性（打桩缝）。写动词不在这张
    面上——空跑不变量靠「Deps 里没有它们」。
    """

    ns_root: Path
    archive_root: Path
    _clock: Callable[[], dt.date]

    def _check_ns(self, ns: str) -> None: ...
    def _check_ns_owner(self, ns: str, identity: str | None, role: str = "reader") -> None: ...
    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]: ...
    def stats(self) -> dict[str, Any]: ...
    def distill_plan(
        self,
        window_days: int = 30,
        min_uses: int = 1,
        min_confidence: float = 0.5,
        ns: str = "_shared",
        reader: str | None = None,
    ) -> dict[str, Any]: ...

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...
    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...
    def _resolve_identity(self, value: str | None, role: str) -> str | None: ...


def _decay_signals(store: DreamDeps, ns: str) -> dict[str, int]:
    """单个 ns 的衰减只读计数。活动区与归档区分开扫，不改文件。

    expired_count：活动区里 valid_until 已过（is_expired）。归档区里同样
    已过的记忆只进 archived_count，不在这里重复计。
    archived_count：该 ns 归档区的条数。
    low_activity_count：活动区里命中 is_decay_candidate 的条数（decay_sweep
    会归档、本路径不归档）。
    """
    now = store._clock()
    expired = 0
    low_activity = 0
    for mem, _path in store._scan_parsed(store.ns_root / ns):
        if is_expired(mem, now):
            expired += 1
        if is_decay_candidate(mem, now):
            low_activity += 1
    archived = sum(1 for _mem, _path in store._scan_parsed(store.archive_root / ns))
    return {
        "expired_count": expired,
        "archived_count": archived,
        "low_activity_count": low_activity,
    }


def _summary(ns: str, plan: dict[str, Any], signals: dict[str, int]) -> str:
    """人类可读一行（只含计数，不含记忆正文）。"""
    return (
        f"dream ns={ns} dry-run: "
        f"candidates={len(plan['candidates'])} "
        f"key_duplicates={len(plan['key_duplicates'])} "
        f"expired={signals['expired_count']} "
        f"archived={signals['archived_count']} "
        f"low_activity={signals['low_activity_count']}"
    )


def dream(
    store: DreamDeps,
    window_days: int = 30,
    min_uses: int = 1,
    min_confidence: float = 0.5,
    ns: str = "_shared",
    reader: str | None = None,
) -> dict[str, Any]:
    """对一个 ns 做只读离线编排，返回报告。

    distill 段就是 distill_plan 的返回值（merge_with / possible_dup_of /
    promotion_candidate / key_duplicates 原样可追溯）。decay_signals 只计数。
    stats_before / stats_after 是全库 stats() 的两次快照；本路径不写，二者相等。
    dry_run 恒为 True（v1 没有写开关）。

    门禁在任何扫描之前：候选带正文，扫私有 ns 须属主（与 distill_plan 同规）。
    """
    store._check_ns(ns)
    reader = store._resolve_identity(reader, "reader")
    store._check_ns_owner(ns, reader)
    stats_before = store.stats()
    plan = store.distill_plan(
        window_days=window_days,
        min_uses=min_uses,
        min_confidence=min_confidence,
        ns=ns,
        reader=reader,
    )
    signals = _decay_signals(store, ns)
    stats_after = store.stats()
    return {
        "ns": ns,
        "dry_run": True,
        "summary": _summary(ns, plan, signals),
        "distill": plan,
        "decay_signals": signals,
        "stats_before": stats_before,
        "stats_after": stats_after,
    }
