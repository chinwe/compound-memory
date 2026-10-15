"""离线编排动词（CLI-only）：dream 只读报告（#69）；dream_apply 显式半自动落库（#72）。

ADR-0001：确定性准备，判断段留在调用方 Agent。dream 只编排既有读路径，
空跑不变量靠 DreamDeps 里没有写动词。dream_apply 不另开写路径：入选动作
一律经 store.distill_apply（origin 仍是 distillation，没有 origin=dream）。
无 LLM，不进 MCP（恰好 5 tool）。

写盘门槛不另算分数。入选只认 distill_plan 已经标出的强信号（见
APPLY_SIGNALS）：merge_with / key_duplicates 在调用方给出正文后可进；
promotion_candidate 还须该条动作显式 promote。possible_dup_of 永远不进
apply 包。valid_until 已过与低活性只出现在 dream 的 decay_signals 里。

建议动作 suggested_action（merge / promote / review，#75）写在 distill_plan
上，dream 的 distill 段原样带出，本模块不重算、不放宽门槛。
negative_salience / contradiction_pairs（#78）同样原样带出：只报告，
不参与 dream_apply 入选。错教训更新仍是新写正文 + 归档源。

衰减计数复用 lifecycle.is_decay_candidate（与 decay_sweep 同一谓词）。
valid_until 已过走 scoring.is_expired，与 stats.expired_active 同一日期规则，
范围收成单个 ns 的活动区。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, overload

from ..model import MEMORY_TYPES, Memory
from ..scoring import is_expired
from .lifecycle import is_decay_candidate
from .validation import check_content_size, check_key

# v1 可进 apply 包的信号（#72 单一定义点）。判定顺序即优先级。
# possible_dup_of 故意不在表内：相似度再高也只报告。
APPLY_SIGNALS = ("merge_with", "key_duplicates", "promotion_candidate")

REASON_POSSIBLE_DUP = "possible_dup_of is report-only"
REASON_PROMOTION_OPT_IN = "promotion_candidate requires explicit opt-in"
REASON_BELOW_PROMOTION = "below promotion threshold"
REASON_NO_STRONG_SIGNAL = "no strong signal"

_ACTION_FIELDS = frozenset({"content", "type", "source_ids", "key", "confidence", "promote"})


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


class DreamApplyDeps(Protocol):
    """dream_apply 实际触碰的 store 面。

    唯一写动词是 distill_apply（经实例属性查找，测试可打桩）。没有
    write / archive / forget / batch——本函数不平行落库。
    """

    def _check_ns(self, ns: str) -> None: ...
    def _check_ns_owner(self, ns: str, identity: str | None, role: str = "reader") -> None: ...
    def stats(self) -> dict[str, Any]: ...
    def distill_plan(
        self,
        window_days: int = 30,
        min_uses: int = 1,
        min_confidence: float = 0.5,
        ns: str = "_shared",
        reader: str | None = None,
    ) -> dict[str, Any]: ...
    def distill_apply(
        self,
        content: str,
        type: str,
        source: str,
        source_ids: list[str],
        ns: str = "_shared",
        key: str | None = None,
        confidence: float | None = None,
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
        f"negative={len(plan['negative_salience'])} "
        f"contradictions={len(plan['contradiction_pairs'])} "
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
    promotion_candidate / key_duplicates / suggested_action /
    negative_salience / contradiction_pairs 原样可追溯，本函数不重算）。
    decay_signals 只计数。负向段与矛盾对不放宽写盘门槛。
    stats_before / stats_after 是全库 stats() 的两次快照；本路径不写，二者相等。
    dry_run 恒为 True。写盘是独立的 dream_apply，不挂在本函数上。

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


def _normalize_actions(actions: Any) -> list[dict[str, Any]]:
    """调用方产物包：每项必须自带正文。缺正文是调用方错误，不是跳过。

    在任何 distill_apply 之前整包校验，避免前半已落库后半才发现包不合法。
    """
    if not isinstance(actions, list) or not actions:
        raise ValueError("dream-apply requires a non-empty action list")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(actions):
        if not isinstance(raw, dict):
            raise ValueError(f"dream-apply action {index} must be an object")
        unknown = sorted(set(raw) - _ACTION_FIELDS)
        if unknown:
            raise ValueError(f"dream-apply action {index} has unknown fields: {unknown}")
        content = raw.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"dream-apply action {index} requires non-empty content")
        check_content_size(content)
        mtype = raw.get("type")
        if mtype not in MEMORY_TYPES:
            raise ValueError(f"type must be one of {MEMORY_TYPES}, got: {mtype!r}")
        source_ids = raw.get("source_ids")
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or not all(isinstance(item, str) and item.strip() for item in source_ids)
        ):
            raise ValueError(f"dream-apply action {index} requires source_ids")
        deduped = list(dict.fromkeys(item.strip() for item in source_ids))
        key = raw.get("key")
        if key is None:
            checked_key: str | None = None
        elif not isinstance(key, str) or not key:
            raise ValueError(f"dream-apply action {index} key must be a non-empty string or omitted")
        else:
            check_key(key)
            checked_key = key
        confidence = raw.get("confidence")
        if confidence is not None:
            if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
                raise ValueError(f"dream-apply action {index} confidence must be a number or omitted")
            confidence = float(confidence)
        promote = raw.get("promote", False)
        if not isinstance(promote, bool):
            raise ValueError(f"dream-apply action {index} promote must be a boolean")
        normalized.append(
            {
                "content": content,
                "type": mtype,
                "source_ids": deduped,
                "key": checked_key,
                "confidence": confidence,
                "promote": promote,
            }
        )
    return normalized


def _merge_id_groups(plan: dict[str, Any]) -> list[set[str]]:
    """主候选里同 type 同 key 且带 merge_with 的 id 组（活性门内的强信号）。"""
    buckets: dict[tuple[str, str], set[str]] = {}
    for cand in plan["candidates"]:
        key = cand["key"]
        others = cand["merge_with"]
        if not key or not others:
            continue
        bucket = buckets.setdefault((cand["type"], key), set())
        bucket.add(cand["id"])
        bucket.update(others)
    return [ids for ids in buckets.values() if len(ids) >= 2]


def _key_id_groups(plan: dict[str, Any]) -> list[set[str]]:
    """key_duplicates 专项段的成员 id 组（不受活性门限制的同 key 强信号）。"""
    return [
        {member["id"] for member in group["members"]}
        for group in plan["key_duplicates"]
        if len(group["members"]) >= 2
    ]


def _promotion_ids(plan: dict[str, Any]) -> set[str]:
    return {cand["id"] for cand in plan["candidates"] if cand["promotion_candidate"]}


def _possible_dup_component(plan: dict[str, Any], ids: set[str]) -> bool:
    """source_ids 恰好被 possible_dup_of 边盖住（纯弱信号，没有外人）。"""
    if len(ids) < 2:
        return False
    linked: set[str] = set()
    for cand in plan["candidates"]:
        if cand["id"] not in ids:
            continue
        partners = ids.intersection(cand["possible_dup_of"])
        if partners:
            linked.add(cand["id"])
            linked.update(partners)
    return linked == ids


def _strong_key_signal(ids: set[str], merge_groups: list[set[str]], key_groups: list[set[str]]) -> str | None:
    """一组 source_ids 落在同一个强信号组里才入选。单条不是合并。"""
    if len(ids) < 2:
        return None
    if any(ids <= group for group in merge_groups):
        return "merge_with"
    if any(ids <= group for group in key_groups):
        return "key_duplicates"
    return None


def _record(action: dict[str, Any], *, signal: str | None = None, reason: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "source_ids": list(action["source_ids"]),
        "type": action["type"],
        "key": action["key"],
        "content": action["content"],
        "confidence": action["confidence"],
    }
    if signal is not None:
        body["signal"] = signal
    if reason is not None:
        body["reason"] = reason
    return body


def _classify(
    plan: dict[str, Any], actions: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], str]], list[dict[str, Any]]]:
    """报告 → 可执行动作。未入选的项带原因留下，不写盘。

    只看 APPLY_SIGNALS。negative_salience 与 contradiction_pairs 不在此列：
    负向或矛盾本身不放宽入选（#78）。同 key 强信号仍按 #72/#75 的旧门槛走。
    """
    merge_groups = _merge_id_groups(plan)
    key_groups = _key_id_groups(plan)
    promo = _promotion_ids(plan)
    admitted: list[tuple[dict[str, Any], str]] = []
    skipped: list[dict[str, Any]] = []
    for action in actions:
        ids = set(action["source_ids"])
        signal = _strong_key_signal(ids, merge_groups, key_groups)
        if signal is not None:
            admitted.append((action, signal))
            continue
        if action["promote"]:
            if ids <= promo:
                admitted.append((action, "promotion_candidate"))
            else:
                skipped.append(_record(action, reason=REASON_BELOW_PROMOTION))
            continue
        if ids <= promo:
            skipped.append(_record(action, reason=REASON_PROMOTION_OPT_IN))
            continue
        if _possible_dup_component(plan, ids):
            skipped.append(_record(action, reason=REASON_POSSIBLE_DUP))
            continue
        skipped.append(_record(action, reason=REASON_NO_STRONG_SIGNAL))
    return admitted, skipped


def _reject_overlapping(admitted: list[tuple[dict[str, Any], str]]) -> None:
    """同一包里两笔入选动作共用源 id 会落出两份产物。整包拒绝，零写盘。"""
    seen: set[str] = set()
    for action, _signal in admitted:
        overlap = seen.intersection(action["source_ids"])
        if overlap:
            raise ValueError(f"dream-apply actions reuse source ids: {sorted(overlap)}")
        seen.update(action["source_ids"])


def _apply_summary(
    ns: str,
    dry_run: bool,
    planned: list[dict[str, Any]],
    applied: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> str:
    """人类可读一行（只含计数，不含记忆正文）。"""
    mode = "dry-run" if dry_run else "apply"
    return (
        f"dream-apply ns={ns} {mode}: "
        f"planned={len(planned)} applied={len(applied)} skipped={len(skipped)}"
    )


def dream_apply(
    store: DreamApplyDeps,
    actions: list[Any],
    source: str,
    window_days: int = 30,
    min_uses: int = 1,
    min_confidence: float = 0.5,
    ns: str = "_shared",
    reader: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """显式半自动落库（#72）。默认 dream 不走这里。

    调用方先给出产物（content + type + source_ids）。过门槛的动作逐条调用
    distill_apply（每条自己的原子提交，不套第二层 batch）。未过门槛的进
    skipped 并写原因。dry_run 只把入选动作放进 planned，不调用 distill_apply。

    不接受 forget，不把 possible_dup_of 放进包，不发明正文，不改 origin。
    私有 ns：reader 与 source 都须是属主（与读扫描 / 写落库同一谓词）。
    """
    store._check_ns(ns)
    resolved_source = store._resolve_identity(source, "source")
    reader = store._resolve_identity(reader, "reader")
    if not isinstance(resolved_source, str) or not resolved_source:
        raise ValueError("dream-apply requires source")
    store._check_ns_owner(ns, reader)
    store._check_ns_owner(ns, resolved_source, role="source")
    prepared = _normalize_actions(actions)
    stats_before = store.stats()
    plan = store.distill_plan(
        window_days=window_days,
        min_uses=min_uses,
        min_confidence=min_confidence,
        ns=ns,
        reader=reader,
    )
    admitted, skipped = _classify(plan, prepared)
    _reject_overlapping(admitted)
    planned: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    if dry_run:
        planned = [_record(action, signal=signal) for action, signal in admitted]
    else:
        for action, signal in admitted:
            result = store.distill_apply(
                content=action["content"],
                type=action["type"],
                source=resolved_source,
                source_ids=list(action["source_ids"]),
                ns=ns,
                key=action["key"],
                confidence=action["confidence"],
            )
            row = _record(action, signal=signal)
            row["result"] = result
            applied.append(row)
    stats_after = store.stats()
    return {
        "ns": ns,
        "dry_run": dry_run,
        "summary": _apply_summary(ns, dry_run, planned, applied, skipped),
        "planned": planned,
        "applied": applied,
        "skipped": skipped,
        "stats_before": stats_before,
        "stats_after": stats_after,
    }
