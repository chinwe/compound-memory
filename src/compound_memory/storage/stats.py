"""统计动词（动词件，#37）：stats 健康度扫描 + 桶函数/桶常量随宿主模块。

桶常量与桶函数是 stats 的领域件（ADR 0003 裁决 6），随动词外移至此；
包级 __init__ re-export 保旧导入名（_uses_bucket / USES_HISTOGRAM_BUCKETS 等）。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from ..model import Memory
from ..scoring import age_days, is_expired

# stats 健康度桶（#8：固定边界保证跨期可比，恒输出全桶）——
# uses 桶按复利语义划线：0=死本金、3=归档存活线（ARCHIVE_USES_THRESHOLD）、10+=高价值；
# confidence 桶：<0.3 低信、0.3-0.6 写入默认带、0.6-0.8 已验证、0.8+ 高置信
USES_HISTOGRAM_BUCKETS = ("0", "1-2", "3-5", "6-9", "10+")
CONFIDENCE_HISTOGRAM_BUCKETS = ("<0.3", "0.3-0.6", "0.6-0.8", "0.8-1.0")
RECENT_WINDOW_DAYS = 7


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
    """ISO 日期落在 [now-days, now] 内；坏日期/未来日期一律 False（坏数据不冒充活性）。

    解析降级共用 scoring.age_days；与 recency_age 同源不同策——这里只看 last_used、
    不回退 created，语义差异留在调用处。"""
    age = age_days(date_str, now)
    return age is not None and 0 <= age <= days


class StatsDeps(Protocol):
    """stats 实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。"""

    ns_root: Path
    archive_root: Path
    _clock: Callable[[], dt.date]

    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]: ...
    def review_queue(self) -> list[str]: ...


def stats(store: StatsDeps) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_ns: dict[str, int] = {}
    uses_hist = {bucket: 0 for bucket in USES_HISTOGRAM_BUCKETS}
    conf_hist = {bucket: 0 for bucket in CONFIDENCE_HISTOGRAM_BUCKETS}
    total, archived, conf_sum = 0, 0, 0.0
    recent_feedback, cross_validated = 0, 0
    distilled_total, distilled_recent = 0, 0
    expired_active = 0
    now = store._clock()
    for base, is_archive in ((store.ns_root, False), (store.archive_root, True)):
        for mem, _path in store._scan_parsed(base):
            total += 1
            if is_archive:
                archived += 1
            elif is_expired(mem, now):
                # 活动区里 valid_until 已过：检索已不可见，但仍躺在活动区待手编更新或蒸馏替换
                expired_active += 1
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
        "review_queue_entries": len(store.review_queue()),
        "uses_histogram": uses_hist,
        "confidence_histogram": conf_hist,
        "recent_feedback_7d": recent_feedback,
        "cross_validated": cross_validated,
        "distilled_total": distilled_total,
        "distilled_recent_7d": distilled_recent,
        "expired_active": expired_active,
    }
