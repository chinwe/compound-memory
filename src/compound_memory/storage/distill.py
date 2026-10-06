"""蒸馏动词（动词件，#37）：distill_plan（候选扫描）/ distill_apply（原子落库）。

确定性段（ADR 0003 / CONTEXT.md: Distillation）：判断与摘要交调用方 Agent，
本模块只做扫描与落库编排。阈值常量随宿主动词模块（裁决 6），__init__
re-export 保旧导入名（CLI --help 文本由常量生成）。

distill_apply 的锁语义逐位保持：源读取在写锁内（batch 持锁）、检查失败
零操作、批尾溯源消息经 batch 句柄覆写；batch 与 write 一律走 store 的
实例属性查找（测试的 racing_patch 打桩缝，#36 沉淀约束）。
"""

from __future__ import annotations

import datetime as dt
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, overload

from ..model import Memory
from ..scoring import doc_text, dup_similarity_matrix, is_expired, recency_age
from .locking import _Batch

# 蒸馏信号阈值（distill-plan 单一定义点；CLI --help 文本由这两个常量生成，不会漂移）：
# 疑似重复 = normalized_similarity(BM25/n_query_tokens) 达到该值；晋升建议 = episode 高活性门槛
DISTILL_DUP_SIM_THRESHOLD = 0.5
PROMOTION_USES_THRESHOLD = 5


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
        project: str | None = None,
    ) -> dict[str, Any]: ...
    def _archive(self, mem: Memory) -> None: ...

    def batch(self, message: str | None = None) -> AbstractContextManager[_Batch]: ...

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...
    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...
    def _resolve_identity(self, value: str | None, role: str) -> str | None: ...


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
    另有 key_duplicates 专项段：同 ns 同 type 同 key 组员 ≥2 的多版本组，
    不受窗口/活性门限制（废置旧版 uses=0 进不了主候选，运维实测盲区）。
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
    key_groups: dict[tuple[str, str], list[Memory]] = {}
    for mem, _path in store._scan_parsed(store.ns_root / ns):
        if is_expired(mem, now):
            continue
        if recency_age(mem, now) is None:
            continue  # 坏日期跳过，与主扫描同规（宁缺勿滥）
        if mem.key:
            key_groups.setdefault((mem.type, mem.key), []).append(mem)
    key_duplicates = [
        {
            "type": mtype,
            "key": mkey,
            "members": [
                {
                    "id": m.id,
                    "created": m.created,
                    "uses": m.uses,
                    "confidence": m.confidence,
                    "content": m.content,
                }
                for m in sorted(members, key=lambda m: (m.created, m.id))
            ],
        }
        for (mtype, mkey), members in sorted(key_groups.items())
        if len(members) >= 2
    ]
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
        "key_duplicates": key_duplicates,
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
    产物与现存 fact/insight 的 key 冲突走既有 review 队列机制，不特殊对待。
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
            project=product_project,
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
