"""蒸馏动词（动词件，#37）：distill_plan（候选扫描）/ distill_apply（原子落库）。

确定性段（ADR 0003 / CONTEXT.md: Distillation）：判断与摘要交调用方 Agent，
本模块只做扫描与落库编排。阈值常量随宿主动词模块（裁决 6），__init__
re-export 保旧导入名（CLI --help 文本由常量生成）。

建议动作（#75）也在本模块单一定义：suggested_action 只有 merge / promote /
review。synthesize 与 supersede 不单列——同 key 多版，以及调用方决定的跨 key
多源综合，都是 merge，簇用 source_ids 标识（不写 supersedes: frontmatter）。
possible_dup_of 只标 review。落库仍只走 distill_apply。

教训报告（#78）也在本模块：不新增记忆类型，不扩 suggested_action。
negative_salience / contradiction_pairs 只标注。借用教训语义，不搬因果引擎。
dream-apply 的入选门槛不因这两段放宽。

distill_apply 的锁语义逐位保持：源读取在写锁内（batch 持锁）、检查失败
零操作、批尾溯源消息经 batch 句柄覆写；batch 与 write 一律走 store 的
实例属性查找（测试的 racing_patch 打桩缝，#36 沉淀约束）。
"""

from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, overload

from ..model import EVIDENCE_CONTRADICTION_COUNT, EVIDENCE_FAILURE_COUNT, Memory, evidence_view, zero_evidence
from ..scoring import doc_text, dup_similarity_matrix, is_expired, recency_age
from .locking import _Batch

# 蒸馏信号阈值（distill-plan 单一定义点；CLI --help 文本由这两个常量生成，不会漂移）：
# 疑似重复 = normalized_similarity(BM25/n_query_tokens) 达到该值；晋升建议 = episode 高活性门槛
DISTILL_DUP_SIM_THRESHOLD = 0.5
PROMOTION_USES_THRESHOLD = 5

# 建议动作词汇 v1（#75 单一定义点）。没有 synthesize / supersede，也没有 avoid（#78）。
SUGGESTED_ACTIONS = ("merge", "promote", "review")

# 教训链接前缀（#78 单一定义点）。扫描器只认这三种；其余 links 仍是自由字符串。
LESSON_LINK_PREFIXES = ("avoid", "fixes", "contradicts")

# 负向显著性（#78 单一定义点）。低于缺省置信度起点算低置信。
# score = failure_count + contradiction_count
#       + 1（lesson_role == avoid）+ 1（自身带 avoid: 链接）
#       + 1（contradicts: 关系的任一端）+ 0.5（confidence < 本阈值）
# 夹具钉 score；改权重即契约变更。fixes: 认前缀，但不进这两段。
NEGATIVE_LOW_CONFIDENCE = 0.5
_SIGNAL_AVOID = "avoid"
_SIGNAL_AVOID_LINK = "avoid_link"
_SIGNAL_FAILURE = "failure_count"
_SIGNAL_LOW_CONFIDENCE = "low_confidence"
_SIGNAL_CONTRADICTION_COUNT = "contradiction_count"
_SIGNAL_CONTRADICTS = "contradicts"


class DistillDeps(Protocol):
    """distill 动词实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。

    _resolve_identity 镜像 facade 的 overload 对（同 ReviewDeps 的理由）；
    write 镜像 facade 完整签名（mypy 结构匹配按位置参数对齐，签名必须同构，
    distill_apply 只用其中 keyword 子集）。
    """

    ns_root: Path
    _clock: Callable[[], dt.date]

    def _check_ns(self, ns: str) -> None: ...
    def _check_ns_owner(self, ns: str, identity: str | None, role: str = "reader") -> None: ...
    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]: ...
    def find(self, mem_id: str) -> Memory | None: ...
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
        evidence: dict[str, Any] | None = None,
        project: str | None = None,
        lesson_role: str | None = None,
    ) -> dict[str, Any]: ...
    def _archive(self, mem: Memory) -> None: ...

    def batch(self, message: str | None = None) -> AbstractContextManager[_Batch]: ...

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...
    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...
    def _resolve_identity(self, value: str | None, role: str) -> str | None: ...


def _candidate_suggested_action(
    *,
    merge_with: list[str],
    promotion_candidate: bool,
    in_weak_dup: bool,
) -> str | None:
    """主候选一行的建议动作。强结构信号优先，没有信号就是 None。

    同 key 的 merge_with 压过晋升与弱相似（多版本先归并）。弱相似——含只被
    对方点名的非对称 BM25 边——只落到 review。
    """
    if merge_with:
        return "merge"
    if promotion_candidate:
        return "promote"
    if in_weak_dup:
        return "review"
    return None


def _shared_value(values: list[Any]) -> Any:
    """全体相同才保留；跨 key / 跨 type 的簇不假装有单一 key 或 type。"""
    if len(set(values)) == 1:
        return values[0]
    return None


def _review_components(candidates: list[dict[str, Any]]) -> list[list[str]]:
    """possible_dup_of 的无向连通分量（规模 ≥2）。

    BM25 矩阵不对称，边按任一方向连通。分量内 id 升序，分量之间按
    source_ids 升序，报告顺序不跟扫描顺序漂。
    """
    ids = [cand["id"] for cand in candidates]
    pos = {cid: index for index, cid in enumerate(ids)}
    parent = list(range(len(ids)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left, root_right = find(left), find(right)
        if root_left == root_right:
            return
        if root_left < root_right:
            parent[root_right] = root_left
        else:
            parent[root_left] = root_right

    linked = [False] * len(ids)
    for cand in candidates:
        origin = pos[cand["id"]]
        for other in cand["possible_dup_of"]:
            dest = pos.get(other)
            if dest is None:
                continue
            union(origin, dest)
            linked[origin] = True
            linked[dest] = True
    buckets: dict[int, list[str]] = {}
    for index, cid in enumerate(ids):
        if not linked[index]:
            continue
        buckets.setdefault(find(index), []).append(cid)
    components = [sorted(members) for members in buckets.values() if len(members) >= 2]
    components.sort()
    return components


def _action_record(
    suggested_action: str,
    source_ids: list[str],
    signal: str,
    mtype: str | None,
    key: str | None,
) -> dict[str, Any]:
    """建议动作簇。merge 一律用 source_ids 标识成员，不靠「是否共享 key」改名。"""
    if suggested_action not in SUGGESTED_ACTIONS:
        raise ValueError(f"suggested_action must be one of {SUGGESTED_ACTIONS}, got: {suggested_action!r}")
    return {
        "suggested_action": suggested_action,
        "source_ids": list(source_ids),
        "signal": signal,
        "type": mtype,
        "key": key,
    }


def _merge_signal(source_ids: list[str], by_id: dict[str, dict[str, Any]]) -> str:
    """整组成员都在活性窗里且互标 merge_with 时记 merge_with，否则记 key_duplicates。"""
    idset = set(source_ids)
    if len(idset) < 2:
        return "key_duplicates"
    for cid in source_ids:
        cand = by_id.get(cid)
        if cand is None or set(cand["merge_with"]) != idset - {cid}:
            return "key_duplicates"
    return "merge_with"


def _suggested_actions(
    candidates: list[dict[str, Any]], key_duplicates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """报告级动作簇：同 key merge 自动列出；promote 只标注；弱相似只 review。

    调用方决定的跨 key 多源综合不在这里另起名字——落库时仍叫 merge，成员就是
    调用方给出的 source_ids。扫描器不会把 possible_dup_of 升级成 merge。
    已被同 key merge 簇盖住的弱相似分量不再重复开一条 review。
    """
    by_id = {cand["id"]: cand for cand in candidates}
    actions: list[dict[str, Any]] = []
    merge_sets: list[set[str]] = []
    for group in key_duplicates:
        source_ids = list(group["source_ids"])
        merge_sets.append(set(source_ids))
        actions.append(
            _action_record(
                "merge",
                source_ids,
                _merge_signal(source_ids, by_id),
                group["type"],
                group["key"],
            )
        )
    for cand in sorted(
        (row for row in candidates if row["suggested_action"] == "promote"),
        key=lambda row: row["id"],
    ):
        actions.append(
            _action_record(
                "promote",
                [cand["id"]],
                "promotion_candidate",
                cand["type"],
                cand["key"],
            )
        )
    for component in _review_components(candidates):
        comp = set(component)
        if any(comp <= group for group in merge_sets):
            continue
        members = [by_id[cid] for cid in component]
        actions.append(
            _action_record(
                "review",
                component,
                "possible_dup_of",
                _shared_value([member["type"] for member in members]),
                _shared_value([member["key"] for member in members]),
            )
        )
    return actions


def parse_lesson_link(link: str) -> tuple[str, str] | None:
    """约定前缀 avoid / fixes / contradicts + 目标 id 或 key。

    前缀必须在字符串开头，目标是单个无空白 token（冒号后的空白会去掉）。
    其余自由字符串——包括正文里碰巧出现这些词——返回 None。
    """
    if not isinstance(link, str):
        return None
    for name in LESSON_LINK_PREFIXES:
        prefix = name + ":"
        if not link.startswith(prefix):
            continue
        target = link[len(prefix):].strip()
        if target and not any(ch.isspace() for ch in target):
            return name, target
        return None
    return None


def _evidence_count(mem: Memory, key: str) -> int:
    """证据计数：只认正整数。坏类型当 0，不让一条脏 frontmatter 炸掉整场扫描。"""
    value = evidence_view(mem).get(key, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _resolve_lesson_targets(
    token: str,
    by_id: dict[str, Memory],
    by_key: dict[str, list[Memory]],
) -> list[Memory]:
    """目标先按 id 命中（至多一条）；否则按 key 命中该 ns 扫描集里的全部。"""
    hit = by_id.get(token)
    if hit is not None:
        return [hit]
    return list(by_key.get(token, []))


def _lesson_sections(mems: list[Memory]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """负向显著性与矛盾对（#78）。只读、确定性，不改 suggested_action。

    扫描集与 key_duplicates 相同：活动区、跳过 valid_until 已过与坏日期，
    不受窗口和活性门限制——否则低置信与 uses=0 的 avoid 会被主候选滤掉。
    """
    by_id = {mem.id: mem for mem in mems}
    by_key: dict[str, list[Memory]] = {}
    for mem in mems:
        if mem.key:
            by_key.setdefault(mem.key, []).append(mem)

    avoid_link_ids: set[str] = set()
    contradict_pairs: set[tuple[str, str]] = set()
    for mem in mems:
        for link in mem.links:
            parsed = parse_lesson_link(link)
            if parsed is None:
                continue
            kind, target = parsed
            if kind == "avoid":
                # 一元信号：这条记忆自己在声明回避，目标解析失败也算。
                avoid_link_ids.add(mem.id)
                continue
            if kind != "contradicts":
                # fixes: 只认前缀。不进负向段，也不当成矛盾对。
                continue
            for other in _resolve_lesson_targets(target, by_id, by_key):
                if other.id == mem.id:
                    continue
                contradict_pairs.add((min(mem.id, other.id), max(mem.id, other.id)))

    endpoints = {cid for pair in contradict_pairs for cid in pair}
    salience: list[dict[str, Any]] = []
    for mem in mems:
        failure_count = _evidence_count(mem, EVIDENCE_FAILURE_COUNT)
        contradiction_count = _evidence_count(mem, EVIDENCE_CONTRADICTION_COUNT)
        signals: list[str] = []
        score = 0.0
        if mem.lesson_role == "avoid":
            signals.append(_SIGNAL_AVOID)
            score += 1.0
        if mem.id in avoid_link_ids:
            signals.append(_SIGNAL_AVOID_LINK)
            score += 1.0
        if failure_count:
            signals.append(_SIGNAL_FAILURE)
            score += failure_count
        if mem.confidence < NEGATIVE_LOW_CONFIDENCE:
            signals.append(_SIGNAL_LOW_CONFIDENCE)
            score += 0.5
        if contradiction_count:
            signals.append(_SIGNAL_CONTRADICTION_COUNT)
            score += contradiction_count
        if mem.id in endpoints:
            signals.append(_SIGNAL_CONTRADICTS)
            score += 1.0
        if not signals:
            continue
        salience.append(
            {
                "id": mem.id,
                "score": score,
                "lesson_role": mem.lesson_role,
                "confidence": mem.confidence,
                "failure_count": failure_count,
                "contradiction_count": contradiction_count,
                "signals": signals,
            }
        )
    salience.sort(key=lambda row: (-row["score"], row["id"]))

    pairs: list[dict[str, Any]] = []
    groups: dict[tuple[str, str], list[Memory]] = {}
    for mem in mems:
        if mem.key:
            groups.setdefault((mem.type, mem.key), []).append(mem)
    for (mtype, key), members in groups.items():
        ordered = sorted(members, key=lambda item: item.id)
        for index, left in enumerate(ordered):
            for right in ordered[index + 1:]:
                if left.content.strip() == right.content.strip():
                    continue
                pairs.append(
                    {
                        "ids": [left.id, right.id],
                        "reason": "same_key",
                        "type": mtype,
                        "key": key,
                    }
                )
    for left_id, right_id in contradict_pairs:
        left, right = by_id[left_id], by_id[right_id]
        pairs.append(
            {
                "ids": [left_id, right_id],
                "reason": "contradicts",
                "type": left.type if left.type == right.type else None,
                "key": left.key if left.key and left.key == right.key else None,
            }
        )
    pairs.sort(key=lambda row: (row["ids"], row["reason"]))
    return salience, pairs


def distill_plan(
    store: DistillDeps,
    window_days: int = 30,
    min_uses: int = 1,
    min_confidence: float = 0.5,
    ns: str = "_shared",
    reader: str | None = None,
) -> dict[str, Any]:
    """蒸馏候选扫描：窗口 + 活性门过滤，产出带信号标注的建议清单（只标注不合并）。

    主候选三类信号：merge_with（同 ns 同 type 同 key，强信号）、possible_dup_of
    （BM25 normalized_similarity ≥ DISTILL_DUP_SIM_THRESHOLD，弱信号）、
    promotion_candidate（episode 高活性，晋升建议——判断后置，#6）。
    每条主候选另有 suggested_action（merge / promote / review / None，#75）。
    另有 key_duplicates 专项段：同 ns 同 type 同 key 组员 ≥2 的多版本组，
    不受窗口/活性门限制（废置旧版 uses=0 进不了主候选，运维实测盲区），
    组上 suggested_action 恒为 merge，并带 source_ids。
    suggested_actions 把上述簇列成一份清单：同 key merge 自动列出（仍要调用方
    正文才落库）；promote 只标注，dream-apply 仍须显式点名；possible_dup_of
    只标 review。跨 key 多源综合不另叫 synthesize，落库时用 merge + source_ids。
    negative_salience / contradiction_pairs（#78）是另两段只读标注：负向条目
    来自 failure_count、低置信、矛盾计数、lesson_role avoid、avoid: 链接与
    contradicts: 关系；矛盾对来自同 key 内容冲突或 contradicts: 链接。
    这两段不受窗口/活性门限制（与 key_duplicates 同人口），不产生新的
    suggested_action，也不放宽 dream-apply。fixes: 只被识别，不进这两段。
    归档区不参与；过期（valid_until 已过）与坏日期记忆按宁缺勿滥跳过。

    reader：候选带正文返回，扫私有 ns 须属主（与 get/search 同规则）。
    """
    store._check_ns(ns)
    reader = store._resolve_identity(reader, "reader")
    store._check_ns_owner(ns, reader)
    now = store._clock()
    cands: list[Memory] = []
    for mem, _path in store._scan_parsed(store.ns_root / ns):
        if is_expired(mem, now):
            continue  # 过期事实不该被蒸馏固化进新产物
        age = recency_age(mem, now)
        if age is None or age > window_days:
            continue
        if mem.uses < min_uses or mem.confidence < min_confidence:
            continue
        cands.append(mem)
    sims = dup_similarity_matrix([doc_text(m) for m in cands])
    by_key: dict[tuple[str, str], list[int]] = {}
    for i, mem in enumerate(cands):
        if mem.key:
            by_key.setdefault((mem.type, mem.key), []).append(i)
    # 同 key 多版本专项（2026-10-05 运维盲区）：清行未归档的废置旧版 uses=0，
    # 会被主候选的 uses≥1 活性门滤出人审视野——专项段不受窗口/活性门限制，
    # 只按「同 ns 同 type 同 key 组员 ≥2」圈出全组成员，判断段据此做归档取舍。
    # 第二遍与 key_duplicates 同人口，教训段复用这份扫描（不再单开第三遍）。
    scanned: list[Memory] = []
    key_groups: dict[tuple[str, str], list[Memory]] = {}
    for mem, _path in store._scan_parsed(store.ns_root / ns):
        if is_expired(mem, now):
            continue
        if recency_age(mem, now) is None:
            continue  # 坏日期跳过，与主扫描同规（宁缺勿滥）
        scanned.append(mem)
        if mem.key:
            key_groups.setdefault((mem.type, mem.key), []).append(mem)
    key_duplicates: list[dict[str, Any]] = []
    for (mtype, mkey), members in sorted(key_groups.items()):
        if len(members) < 2:
            continue
        ordered = sorted(members, key=lambda m: (m.created, m.id))
        key_duplicates.append(
            {
                "type": mtype,
                "key": mkey,
                "suggested_action": "merge",
                "source_ids": [m.id for m in ordered],
                "members": [
                    {
                        "id": m.id,
                        "created": m.created,
                        "uses": m.uses,
                        "confidence": m.confidence,
                        "content": m.content,
                    }
                    for m in ordered
                ],
            }
        )
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
    incoming: dict[str, bool] = {cand["id"]: False for cand in candidates}
    for cand in candidates:
        for other in cand["possible_dup_of"]:
            if other in incoming:
                incoming[other] = True
    for cand in candidates:
        cand["suggested_action"] = _candidate_suggested_action(
            merge_with=cand["merge_with"],
            promotion_candidate=cand["promotion_candidate"],
            in_weak_dup=bool(cand["possible_dup_of"]) or incoming[cand["id"]],
        )
    negative_salience, contradiction_pairs = _lesson_sections(scanned)
    return {
        "window_days": window_days,
        "min_uses": min_uses,
        "min_confidence": min_confidence,
        "ns": ns,
        "candidates": candidates,
        "key_duplicates": key_duplicates,
        "suggested_actions": _suggested_actions(candidates, key_duplicates),
        "negative_salience": negative_salience,
        "contradiction_pairs": contradiction_pairs,
    }


def distill_apply(
    store: DistillDeps,
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
    产物与现存 fact/insight/decision 的 key 冲突走既有 review 队列机制，不特殊对待。
    """
    source = store._resolve_identity(source, "source")
    source_ids = list(dict.fromkeys(source_ids))  # 去重保序：重复源只归档一次
    with store.batch() as batch_ctx:
        # 源读取在写锁内（batch 持锁）：find 在锁外时，间隙内并发 feedback
        # 的 uses/confidence 会被旧快照在归档写回时覆盖丢失（写动词
        # 读-改-写全程持锁的自查条款）。检查失败零操作，批尾不产生提交。
        sources = [store.find(mid) for mid in source_ids]
        missing = [mid for mid, mem in zip(source_ids, sources) if mem is None]
        if missing:
            return {"found": False, "missing": missing}
        # 蒸馏不跨 ns：私有记忆被当源蒸进 _shared 是正文泄漏通道；
        # distill_plan 本就按单 ns 扫描，源与产物同 ns 是既定流程
        foreign_ns = sorted({s.ns for s in sources if s is not None and s.ns != ns})
        if foreign_ns:
            raise ValueError(f"distill sources must live in target ns {ns!r}; found in: {foreign_ns}")
        # 蒸馏产物继承源的 project（ADR 0010，同项目提纯）。可见面包含不变量：
        # 产物可见面必须 ⊆ 源可见面——任一源已标注项目 ⇒ 产物必须跟着标注
        # （全局产物会把项目源的内容泄进全局会话）；全部源为全局 ⇒ 产物全局。
        # 源标注了两个不同项目则无单一适用域，显式拒绝（与跨 ns 拒绝同型），
        # 让调用方按项目拆分蒸馏。
        distinct_projects = sorted({s.project for s in sources if s is not None and s.project is not None})
        if len(distinct_projects) > 1:
            raise ValueError(f"distill sources must share one project scope; found: {distinct_projects}")
        product_project = distinct_projects[0] if distinct_projects else None
        result = store.write(
            content,
            type=type,
            source=source,
            ns=ns,
            key=key,
            links=source_ids,
            confidence=confidence,
            origin="distillation",
            evidence=zero_evidence(),  # ADR-0008：产物证据显式零起点，不继承源计数（双重计数）
            project=product_project,  # ADR 0010：产物继承源的（唯一）project 值
        )
        archived: list[str] = []
        for src in sources:
            assert src is not None
            if not src.archived:
                store._archive(src)
            archived.append(src.id)
        batch_ctx.message = f"distill apply {result['id']} <- " + ", ".join(archived)
    result["found"] = True
    result["archived_sources"] = archived
    return result
