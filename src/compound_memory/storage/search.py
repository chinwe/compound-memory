"""检索动词（动词件，#37）：search / vector_recall / active_neighbors /
candidates / scored_candidates / lexical_candidates + VEC_POOL。

检索 = 选候选（store 的 layout 职责）+ 排序（scoring.rank 单一定义点），
结果形状逐位不变（契约 test_search 钉住）。#41 候选快路径：词法缓存给出
per-doc token 统计的候选免 parse 进 rank（宽查询大候选集的成本大头是逐
候选 yaml parse），缓存无条目的候选回退 parse——两条路径输出逐位一致。
门禁执行时序不动：search 是参数型动词，ns 校验在函数入口（ADR 0003
裁决 5）。VEC_POOL 随宿主动词（裁决 6）单一定义于此；包级公开面收窄
（#39）后不再 re-export，storage.search 是唯一导入路径（见 __init__ docstring）。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Callable, Protocol, overload

from ..index import Index
from ..liveness import ScanWindow
from ..model import Memory, evidence_view
from ..scoring import DocStats, expired_by_date, is_expired, rank, tokenize
from ..vector_index import VectorIndex
from .validation import check_project

# storage 域告警的单点 logger：向量降级（#19）的观测出口
logger = logging.getLogger(__name__)

# 向量召回候选池：词面候选 ∪ 向量 KNN 前 VEC_POOL 条（ns/活性过滤后）
VEC_POOL = 16


def in_project_scope(mem_project: str | None, project: str | None) -> bool:
    """project 适用性谓词（ADR 0010，检索过滤的收口单点）：字段为空 = 全局通用，
    任何读方可见；已标注 = 仅声明同一项目的读方可见。读方未声明项目
    （project=None）⇒ 只见全局（fail-closed：「没声明项目 = 全局会话」，
    项目记忆不外溢；与私有 ns 忘带 reader 即拒的诚实缺省同型）。

    该谓词只做适用性判定，可见性/属主判定（ns 轴）先行不变——私有 ns ∩
    project 是两级门依次收窄，不在此合并。"""
    return mem_project is None or mem_project == project


class SearchDeps(Protocol):
    """search 动词实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。

    _resolve_identity 镜像 facade 的 overload 对（同 ReviewDeps 的理由）。
    parse 是 staticmethod，经实例属性查找调用（store.parse(path)）。
    """

    root: Path
    index: Index
    vector_index: VectorIndex
    _clock: Callable[[], dt.date]
    _embedder: Callable[[list[str]], list[list[float]]] | None
    _scan_window: ScanWindow

    def parse(self, path: Path) -> Memory: ...
    def find(self, mem_id: str) -> Memory | None: ...
    def _check_ns(self, ns: str) -> None: ...
    def _check_ns_owner(self, ns: str, identity: str | None, role: str = "reader") -> None: ...

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...
    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...
    def _resolve_identity(self, value: str | None, role: str) -> str | None: ...


def search(
    store: SearchDeps,
    query: str,
    ns: str | None = None,
    top_k: int = 5,
    include_neighbors: bool = True,
    reader: str | None = None,
    project: str | None = None,
    explain: bool = False,
) -> list[dict[str, Any]]:
    """检索 = 选候选（store 的 layout 职责）+ 排序（scoring.rank 单一定义点）。

    embedder 可用时叠加向量召回：候选 = 词面命中 ∪ 向量 KNN（ns/活性过滤），
    两路 rank 在 rank 内 RRF 融合；向量路任何故障都降级纯词面（宁缺勿炸）。

    ns=None（缺省）为双通道检索：_shared ∪ 调用方自有私有 ns（agent-<reader>，
    身份已知时）——私有条目天然出现在结果里，保障不依赖调用方记得显式补搜
    （无身份时退化为单 _shared，与旧版缺省一致）；显式传 ns 保持单 ns 精确语义。
    reader 是调用方身份，ns=agent-* 时必填且须为属主（读侧 owner 校验，
    与 write 的越权抛 PermissionError 对称）。

    project 是调用方工作区的适用性上下文（ADR 0010，fail-closed）：不传只见
    全局（字段为空）记忆，传了见 全局 ∪ 该项目——过滤收口在候选层单点
    （in_project_scope），词面/向量/邻居三路候选同用；拼错的 slug 响亮抛
    ValueError（与非法 ns 同规，静默空结果会让调用方误判「无相关记忆」）。

    explain 是 opt-in 排障面（#44，ADR-0008 展示边界）：True 时每个 hit 附加
    `explain` 排序分量对象（rank 单点产出）与 `evidence` 证据摘要行
    （evidence_summary）；缺省 False 返回形状逐位不变——分量只在请求时付费。
    """
    if top_k < 0:
        raise ValueError(f"top_k must be >= 0, got: {top_k}")
    check_project(project)
    if ns is None:
        reader = store._resolve_identity(reader, "reader")
        scopes = ["_shared"]
        if reader:
            private = f"agent-{reader.removeprefix('agent-')}"
            # 私有侧派生自 reader，属主校验恒真但按"读正文先过门"的不变量照走，
            # 防未来派生逻辑变化时静默放行
            store._check_ns_owner(private, reader)
            scopes.append(private)
    else:
        store._check_ns(ns)
        reader = store._resolve_identity(reader, "reader")
        store._check_ns_owner(ns, reader)
        scopes = [ns]
    now = store._clock()
    q_tokens = tokenize(query)
    if not q_tokens:
        return []
    # 读动词开 scan 共享窗口：向量 KNN 与词法候选两路对账共用一遍 scan（#41）
    store._scan_window.open()
    vec_sims, vec_rels = vector_recall(store, query, set(scopes), now, project)
    mems, stats, rels_by_id = scored_candidates(store, q_tokens, set(scopes), vec_rels, now, project)
    hits = rank(
        query,
        mems,
        now=now,
        top_k=top_k,
        neighbor_lookup=(
            lambda mid: active_neighbors(store, mid, set(scopes), now, project)
        ) if include_neighbors else None,
        vec_sims=vec_sims,
        doc_stats=stats,
        # 缓存候选的 content 为占位空串：命中条目的正文按 rel 现 parse（top_k 次，
        # 与全候选 parse 相比可忽略）；id→rel 映射由 scored_candidates 给全
        content_loader=(lambda mid: store.parse(store.root / rels_by_id[mid]).content) if rels_by_id else None,
        explain=explain,
    )
    if explain and hits:
        # 证据摘要行（#44）：只对返回的 top_k 现 parse（与 content_loader 同界，
        # 与候选集大小无关）；hits ⊆ 候选、rels_by_id 恒可定位——防御式 .get
        # 只是「检索降级不报错」的一致性，不构成预期路径。缓存条目不携带证据块
        # （_doc_entry 固定键集），按 id 视图的全量明细走 explain 动词。
        for hit in hits:
            rel = rels_by_id.get(hit["id"])
            if rel is not None:
                hit["evidence"] = evidence_summary(store.parse(store.root / rel))
    return hits


def evidence_summary(mem: Memory) -> dict[str, Any]:
    """单条记忆的证据摘要行（ADR-0008 展示边界的检索面投影，#44）：计数三元组 +
    last_verified + origin 派生标记。经 evidence_view 惰性迁移（无块旧记忆读为
    success_count=uses，读路径不落块）；有界输出——recent 明细与 validated_by
    全量只走按 id 的 explain 视图，检索面不重复正文与长明细。蒸馏产物
    （origin=distillation）读出显式零起点 {0,0,0}——「不继承源证据」在检索面可辨。
    """
    ev = evidence_view(mem)
    return {
        "origin": mem.origin,
        "success_count": ev.get("success_count", 0),
        "failure_count": ev.get("failure_count", 0),
        "contradiction_count": ev.get("contradiction_count", 0),
        "last_verified": ev.get("last_verified"),
    }


def explain(store: SearchDeps, mem_id: str, reader: str | None = None) -> dict[str, Any]:
    """按 id 证据视图（ADR-0008 展示边界，#44）：evidence_summary 的按 id 全量形态。

    单条记忆的置信度构成：证据块计数（惰性迁移视图）+ last_verified + recent
    明细（cap 10，ADR-0007）+ validated_by 跨宿主验证明细（ADR-0008：独立证据
    = 不同宿主的直接使用证据，source 自验不加成）+ origin/derived 派生标记
    （蒸馏产物 derived=True——证据显式零起点、不回流源）+ conf/uses 当前值。
    读路径零提交；门禁同 get（凡返回记忆元数据的新入口先过身份门：私有 ns
    仅属主，reader 角色；按 id 动词门禁在 find 之后）。入口仅 store + CLI——
    MCP 恰好 5 tool 红线不动，单条证据视图不经 memory_get 扩参（#44 载体裁决）。
    """
    reader = store._resolve_identity(reader, "reader")
    mem = store.find(mem_id)
    if mem is None:
        return {"found": False}
    store._check_ns_owner(mem.ns, reader)
    ev = evidence_view(mem)
    return {
        "found": True,
        "id": mem.id,
        "ns": mem.ns,
        "type": mem.type,
        "source": mem.source,
        "archived": mem.archived,
        "confidence": mem.confidence,
        "uses": mem.uses,
        "origin": mem.origin,
        "derived": mem.origin == "distillation",
        "validated_by": list(mem.validated_by),
        "evidence": {
            "success_count": ev.get("success_count", 0),
            "failure_count": ev.get("failure_count", 0),
            "contradiction_count": ev.get("contradiction_count", 0),
            "last_verified": ev.get("last_verified"),
            "recent": [dict(d) for d in ev.get("recent", [])],
        },
    }


def vector_recall(
    store: SearchDeps, query: str, nss: set[str], now: dt.date, project: str | None = None
) -> tuple[dict[str, float] | None, list[str]]:
    """向量召回：查询编码 + KNN（大池取回后按 ns 集合/去重收敛到 VEC_POOL）。

    返回 (vec_sims, vec_rels)；embedder 未注入或任何故障 ⇒ (None, []) 纯词面降级。
    过期记忆与归档同等排除——候选并集两侧同一套活性语义，不给过期记忆留向量旁路；
    project 适用性同此（ADR 0010）：向量路召回的项目记忆按同一谓词滤除，
    不给适用性过滤留旁路。"""
    if store._embedder is None:
        return None, []
    try:
        qvec = store._embedder([query])[0]
        hits = store.vector_index.knn(qvec, k=64)
        sims: dict[str, float] = {}
        rels: list[str] = []
        for mem_id, rel_path, cos in hits:
            if len(sims) >= VEC_POOL:
                break
            if mem_id in sims:
                continue
            # knn 自带活动区 rel_path，直读即可——逐 hit find() 是 rglob 全库
            # 递归，千条库一次 search 最多 17 遍全扫描（perf-bench 基线的词面
            # 线性项主因）；rel_path 过期（手编挪位）由 knn 内部的 stale 对账修正
            path = store.root / rel_path
            if not path.exists():
                continue
            mem = store.parse(path)
            if mem.archived or mem.ns not in nss or is_expired(mem, now):
                continue
            if not in_project_scope(mem.project, project):
                continue
            sims[mem_id] = cos
            rels.append(rel_path)
        return (sims or None), rels
    except Exception as exc:
        # 宁缺勿炸的降级语义不变；但故障必须可观测（#19）——否则索引损坏/
        # 模型异常会长期被掩盖在「正常降级」里。embedder 未注入是配置路径，
        # 不经此处，不产生日志。
        logger.warning("vector recall degraded to lexical: %s", exc)
        return None, []


def active_neighbors(
    store: SearchDeps, mem_id: str, nss: set[str], now: dt.date, project: str | None = None
) -> list[Memory]:
    """邻居召回的数据源：hit 的一度 links，归档/过期邻居不召回（截断/上限/去环归 rank）。

    ns 集合过滤是访问控制的一部分，不可省：_shared 记忆若链到 agent-* 私有记忆，
    邻居会把私有正文带进调用方不可见的检索结果（2026-10-03 实测泄漏）；
    集合由 search 按"调用方可见的 ns"圈定（双通道 = _shared ∪ 自有私有 ns）。
    project 适用性同此（ADR 0010）：邻居带出按读方 project 滤除（与 get 的
    邻居过滤同规，否则项目记忆经邻居旁路泄漏进全局会话）。
    """
    mem = store.find(mem_id)
    if mem is None:
        return []
    out: list[Memory] = []
    for link_id in mem.links:
        neighbor = store.find(link_id)
        if (
            neighbor is not None
            and not neighbor.archived
            and not is_expired(neighbor, now)
            and neighbor.ns in nss
            and in_project_scope(neighbor.project, project)
        ):
            out.append(neighbor)
    return out


def lexical_candidates(
    store: SearchDeps, q_tokens: list[str], nss: set[str], reader: str | None = None,
    project: str | None = None,
) -> list[Memory]:
    """公开的词面候选通道：按 query token 取索引命中的活动记忆（正文在内）。

    凡返回记忆正文的新入口都过身份门：agent-* 必须属主（与 search/get 同一
    规则），_shared 无需身份。extraction 的复述标注（_dup_of）与未来的批量
    复述检测走此正门，勿直取 candidates 私有件。project 适用性过同套门禁
    （ADR 0010）：不传只见全局——extraction 缺省调用即全局视野，不构成旁路。
    """
    reader = store._resolve_identity(reader, "reader")
    check_project(project)
    for ns in nss:
        store._check_ns(ns)
        store._check_ns_owner(ns, reader)
    return candidates(store, q_tokens, nss, project=project)


def _mem_from_entry(entry: dict[str, Any]) -> Memory:
    """缓存条目 → 候选视图 Memory（content 为占位空串）。

    只覆盖 rank 消费的字段（排序先验 + emit 身份）；命中条目的正文经
    content_loader 现取（top_k 次 parse），不在缓存里复制正文。字段损坏
    （缺键/类型异）抛 KeyError/TypeError/ValueError，由调用方回退 parse。
    """
    return Memory(
        id=entry["id"],
        ns=entry["ns"],
        type=entry["type"],
        source=entry["source"],
        created=entry["created"],
        content="",
        confidence=float(entry["confidence"]),
        uses=int(entry.get("uses", 0)),
        last_used=entry.get("last_used"),
        valid_until=entry.get("valid_until"),
        # 严格取键：条目缺 project（旧版缓存/损坏）⇒ KeyError 回退 parse 读真值，
        # 绝不静默当全局——那会让带 project 的记忆在快路径被多放行（fail-closed 破洞）
        project=entry["project"],
    )


def _merged_candidate_rels(
    store: SearchDeps, q_tokens: list[str], vec_rels: list[str] | None
) -> list[str]:
    """候选骨架（candidates/scored_candidates 共享）前半：rels 并集——
    词法索引命中在前，向量 KNN 命中（去重）排尾。"""
    rels = list(store.index.candidates(q_tokens))
    for rel_path in vec_rels or []:
        if rel_path not in rels:
            rels.append(rel_path)
    return rels


def _live_rel_path(store: SearchDeps, rel: str, prefixes: tuple[str, ...]) -> Path | None:
    """候选骨架后半：ns 前缀剪枝（parse 之前省掉越界 parse）+ 文件存在复查
    （防索引词条与手编文件的漂移）；越界或文件缺失返回 None。"""
    if not rel.startswith(prefixes):
        return None
    path = store.root / rel
    return path if path.exists() else None


def scored_candidates(
    store: SearchDeps,
    q_tokens: list[str],
    nss: set[str],
    vec_rels: list[str] | None = None,
    now: dt.date | None = None,
    project: str | None = None,
) -> tuple[list[Memory], list[DocStats | None], dict[str, str]]:
    """检索的候选来源（#41 快路径 + parse 回退），返回三元组：

    (candidates, 对齐的 per-doc DocStats——回退位为 None, id → rel 映射)。

    词法缓存（tokens.json v2 docs）有条目的 rel 免 parse：token 频表直接供
    rank 的 BM25，先验（confidence/uses/日期/type）与身份从条目重建 Memory
    视图；过期经条目 valid_until 判（expired_by_date 与 is_expired 同源）。
    无条目（向量路独有召回、条目字段损坏）回退 parse——与旧候选路径行为
    逐位一致。ns 前缀剪枝与文件存在性复查照旧：防的是索引与手编文件的漂移。
    project 适用性过滤（ADR 0010）在候选产出前的唯一收口：条目路径与 parse
    路径输出的 Memory 视图都过同一谓词，两条路径过滤语义逐位一致。
    """
    prefixes = tuple(f"namespaces/{ns}/" for ns in nss)
    rels = _merged_candidate_rels(store, q_tokens, vec_rels)
    entries = store.index.doc_entries(rels)
    out: list[Memory] = []
    stats: list[DocStats | None] = []
    rels_by_id: dict[str, str] = {}
    for rel in rels:
        path = _live_rel_path(store, rel, prefixes)
        if path is None:
            continue
        entry = entries.get(rel)
        mem: Memory | None = None
        stat: DocStats | None = None
        if entry is not None:
            try:
                mem = _mem_from_entry(entry)
                tf = entry["tf"]
                if not isinstance(tf, dict):
                    raise TypeError("entry tf is not a mapping")  # 手编损坏条目：降级 parse
                stat = DocStats(tf=tf, dl=int(entry["len"]))
                if now is not None and expired_by_date(entry.get("valid_until"), now):
                    continue
            except (KeyError, TypeError, ValueError):
                mem = None  # 条目字段损坏：降级 parse（检索降级不报错）
        if mem is None:
            mem = store.parse(path)
            if mem.archived or (now is not None and is_expired(mem, now)):
                continue
            stat = None
        if not in_project_scope(mem.project, project):
            continue
        out.append(mem)
        stats.append(stat)
        rels_by_id[mem.id] = rel
    return out, stats, rels_by_id


def candidates(
    store: SearchDeps,
    q_tokens: list[str],
    nss: set[str],
    vec_rels: list[str] | None = None,
    now: dt.date | None = None,
    project: str | None = None,
) -> list[Memory]:
    """Indexed lookup（parse 路径，lexical_candidates 的数据源）：索引活性
    （跨进程重载/带外重建）由各缓存内部自愈，
    这里全信索引命中，只逐一复查文件存在性与 ns 集合/活性——防的是索引
    词条与手编文件内容的漂移（改内容不改目录 mtime，那条路走显式 rebuild）。
    vec_rels 非空时，向量 KNN 命中（rel_path 由向量缓存给出）并入候选并集。
    now 提供时同步排除过期记忆（valid_until 已过 ⇒ 检索不可见，get 不受限）；
    project 适用性过滤（ADR 0010）与活性同点：不匹配读方上下文的记忆不进候选。
    ns 前缀剪枝在 parse 之前：活动区 rel 必为 namespaces/<ns>/...（索引不收
    归档），常见词命中近全库的大库上把 ns 过滤提前省掉全部越界 parse。"""
    prefixes = tuple(f"namespaces/{ns}/" for ns in nss)
    rels = _merged_candidate_rels(store, q_tokens, vec_rels)
    out: list[Memory] = []
    for rel in rels:
        path = _live_rel_path(store, rel, prefixes)
        if path is None:
            continue
        mem = store.parse(path)
        if (
            not mem.archived
            and not (now is not None and is_expired(mem, now))
            and in_project_scope(mem.project, project)
        ):
            out.append(mem)
    return out
