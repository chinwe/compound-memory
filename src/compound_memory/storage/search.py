"""检索动词（动词件，#37）：search / vector_recall / active_neighbors /
candidates / scored_candidates / lexical_candidates + VEC_POOL。

检索 = 选候选（store 的 layout 职责）+ 排序（scoring.rank 单一定义点），
结果形状逐位不变（契约 test_search 钉住）。可见性合取（archived /
valid_until 已过 / ns 集合 / project 适用性）单点在 visibility.is_retrievable，
词面候选、向量召回、邻居召回都走它。#41 候选快路径：词法缓存给出
per-doc token 统计的候选免 parse 进 rank（宽查询大候选集的成本大头是逐
候选 yaml parse），缓存无条目的候选回退 parse——两条路径输出逐位一致，
过滤只在共享候选流里判定一次。门禁执行时序不动：search 是参数型动词，
ns 校验在函数入口（ADR 0003 裁决 5）。VEC_POOL 随宿主动词（裁决 6）
单一定义于此；包级公开面收窄（#39）后不再 re-export，storage.search 是
唯一导入路径（见 __init__ docstring）。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, overload

from ..index import Index
from ..liveness import ScanWindow
from ..model import (
    EVIDENCE_CONTRADICTION_COUNT,
    EVIDENCE_FAILURE_COUNT,
    EVIDENCE_LAST_VERIFIED,
    EVIDENCE_RECENT,
    EVIDENCE_SUCCESS_COUNT,
    Memory,
    evidence_view,
)
from ..scoring import DocStats, rank, tokenize
from ..vector_index import VectorIndex
from .validation import check_project
from .visibility import is_retrievable

# storage 域告警的单点 logger：向量降级（#19）的观测出口
logger = logging.getLogger(__name__)

# 向量召回候选池：词面候选 ∪ 向量 KNN 前 VEC_POOL 条（ns/活性过滤后）
VEC_POOL = 16


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
    全局（字段为空）记忆，传了见 全局 ∪ 该项目——过滤收口在
    visibility.is_retrievable，词面/向量/邻居三路候选同用；拼错的 slug 响亮抛
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
        "success_count": ev.get(EVIDENCE_SUCCESS_COUNT, 0),
        "failure_count": ev.get(EVIDENCE_FAILURE_COUNT, 0),
        "contradiction_count": ev.get(EVIDENCE_CONTRADICTION_COUNT, 0),
        "last_verified": ev.get(EVIDENCE_LAST_VERIFIED),
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
            "success_count": ev.get(EVIDENCE_SUCCESS_COUNT, 0),
            "failure_count": ev.get(EVIDENCE_FAILURE_COUNT, 0),
            "contradiction_count": ev.get(EVIDENCE_CONTRADICTION_COUNT, 0),
            "last_verified": ev.get(EVIDENCE_LAST_VERIFIED),
            "recent": [dict(d) for d in ev.get(EVIDENCE_RECENT, [])],
        },
    }


def vector_recall(
    store: SearchDeps, query: str, nss: set[str], now: dt.date, project: str | None = None
) -> tuple[dict[str, float] | None, list[str]]:
    """向量召回：查询编码 + KNN（大池取回后按 ns 集合/去重收敛到 VEC_POOL）。

    返回 (vec_sims, vec_rels)；embedder 未注入或任何故障 ⇒ (None, []) 纯词面降级。
    valid_until 已过与归档同等排除——候选并集两侧同一套活性语义，不给已过记忆留向量旁路；
    project 适用性同此（ADR 0010）：向量路召回的项目记忆按 is_retrievable 滤除，
    不给适用性过滤留旁路。"""
    if store._embedder is None:
        return None, []
    try:
        qvec = store._embedder([query])[0]
        hits = store.vector_index.knn(qvec, k=64)
        # knn 内部把引擎故障记成 degraded / engine-unavailable 并返回空，
        # 这里不要把它当成「向量路启用但零命中」——否则 stats 仍报 vector。
        if store.vector_index.retrieval_status()["mode"] != "vector":
            return None, []
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
            # 检索面：归档不进向量候选（include_archived 缺省 False）
            if not is_retrievable(mem, nss=nss, now=now, project=project, include_archived=False):
                continue
            sims[mem_id] = cos
            rels.append(rel_path)
        return (sims or None), rels
    except Exception as exc:
        # 宁缺勿炸的降级语义不变；但故障必须可观测（#19）——否则索引损坏/
        # 模型异常会长期被掩盖在「正常降级」里。embedder 未注入是配置路径，
        # 不经此处，不产生日志。
        logger.warning("vector recall degraded to lexical: %s", exc)
        store.vector_index.note_degraded("degraded")
        return None, []


def active_neighbors(
    store: SearchDeps, mem_id: str, nss: set[str], now: dt.date, project: str | None = None,
    include_archived: bool = False,
) -> list[Memory]:
    """邻居召回的数据源（唯一单点，get 与 search 两个面共用）：
    hit 的一度 links。可见性只经 is_retrievable 判定一次。

    归档邻居按「面」分策略（两个面真实不同策略，参数即 seam）：检索面（search）
    缺省 include_archived=False——检索只呈现活动知识；显式寻址面（get）
    include_archived=True 保留——归档是「暂不检索但仍可信」的知识，蒸馏产物
    溯源（test_distill 钉）依赖它。valid_until 已过的邻居两个面都不召回
    （CONTEXT.md：valid_until 次日起退出邻居召回）。

    ns 集合过滤是访问控制的一部分，不可省：_shared 记忆若链到 agent-* 私有记忆，
    邻居会把私有正文带进调用方不可见的检索结果（2026-10-03 实测泄漏）；
    集合由调用方按"读者可见的 ns"圈定（search 双通道 = _shared ∪ 自有私有 ns，
    get = 命中记忆自身 ns）。
    project 适用性同此（ADR 0010）：邻居带出按读方 project 滤除，否则项目记忆
    经邻居旁路泄漏进全局会话。
    """
    mem = store.find(mem_id)
    if mem is None:
        return []
    out: list[Memory] = []
    for link_id in mem.links:
        neighbor = store.find(link_id)
        if neighbor is not None and is_retrievable(
            neighbor,
            nss=nss,
            now=now,
            project=project,
            include_archived=include_archived,
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
    复述检测走此正门，勿直取 candidates 私有件。可见性走共享候选流的
    is_retrievable（ADR 0010）：不传 project 只见全局——extraction 缺省调用
    即全局视野，不构成旁路。now 不传，不按 valid_until 排除。
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


def _view_from_entry(entry: dict[str, Any]) -> tuple[Memory, DocStats]:
    """缓存条目 → (候选视图, DocStats)。字段损坏抛 KeyError/TypeError/ValueError。"""
    mem = _mem_from_entry(entry)
    tf = entry["tf"]
    if not isinstance(tf, dict):
        raise TypeError("entry tf is not a mapping")  # 手编损坏条目：降级 parse
    return mem, DocStats(tf=tf, dl=int(entry["len"]))


def _merged_candidate_rels(
    store: SearchDeps, q_tokens: list[str], vec_rels: list[str] | None
) -> list[str]:
    """候选骨架前半：rels 并集——词法索引命中在前，向量 KNN 命中（去重）排尾。"""
    rels = list(store.index.candidates(q_tokens))
    for rel_path in vec_rels or []:
        if rel_path not in rels:
            rels.append(rel_path)
    return rels


def _live_rel_path(store: SearchDeps, rel: str, prefixes: tuple[str, ...]) -> Path | None:
    """ns 前缀剪枝（加载之前省掉越界 parse）+ 文件存在复查
    （防索引词条与手编文件的漂移）；越界或文件缺失返回 None。

    前缀剪枝是 parse 前的加速，与 is_retrievable 的 ns 轴对齐；语义判定
    仍以谓词为准（frontmatter 的 ns 与目录不一致时谓词排除）。"""
    if not rel.startswith(prefixes):
        return None
    path = store.root / rel
    return path if path.exists() else None


def _load_candidate(
    store: SearchDeps, path: Path, entry: dict[str, Any] | None
) -> tuple[Memory, DocStats | None]:
    """加载一条候选视图。entry 可用且字段完好时免 parse（stat 对齐）；
    无条目或字段损坏时 parse，stat 为 None。这里不做可见性过滤。"""
    if entry is not None:
        try:
            return _view_from_entry(entry)
        except (KeyError, TypeError, ValueError):
            pass  # 条目字段损坏：降级 parse（检索降级不报错）
    return store.parse(path), None


def _candidate_stream(
    store: SearchDeps,
    q_tokens: list[str],
    nss: set[str],
    vec_rels: list[str] | None,
    now: dt.date | None,
    project: str | None,
    *,
    scored: bool,
) -> Iterator[tuple[Memory, DocStats | None, str]]:
    """词面 ∪ 向量的唯一内部候选流。可见性只经 is_retrievable 判定一次。

    scored=True（scored_candidates）：缓存条目免 parse，损坏回退 parse，
    DocStats 与候选对齐。scored=False（candidates / lexical_candidates）：
    一律 parse——公开词面通道要正文，缓存视图的 content 是占位空串。
    两条加载路径产出的 Memory 都过同一谓词（检索面 include_archived=False）。
    """
    prefixes = tuple(f"namespaces/{ns}/" for ns in nss)
    rels = _merged_candidate_rels(store, q_tokens, vec_rels)
    # 只在快路径读 docs：parse 路径不额外触发一次缓存自愈
    entries = store.index.doc_entries(rels) if scored else {}
    for rel in rels:
        path = _live_rel_path(store, rel, prefixes)
        if path is None:
            continue
        mem, stat = _load_candidate(store, path, entries.get(rel) if scored else None)
        if not is_retrievable(mem, nss=nss, now=now, project=project, include_archived=False):
            continue
        yield mem, stat, rel


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

    加载与过滤都在 `_candidate_stream(scored=True)`：词法缓存有条目的 rel
    免 parse，token 频表直接供 rank 的 BM25，先验与身份从条目重建 Memory
    视图；无条目或字段损坏回退 parse。valid_until 已过经视图上的
    is_expired（与条目 valid_until 同源）在 is_retrievable 里排除。
    """
    out: list[Memory] = []
    stats: list[DocStats | None] = []
    rels_by_id: dict[str, str] = {}
    for mem, stat, rel in _candidate_stream(
        store, q_tokens, nss, vec_rels, now, project, scored=True
    ):
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
    """Indexed lookup（parse 路径，lexical_candidates 的数据源）。

    与 scored_candidates 共用 `_candidate_stream`（scored=False：一律 parse，
    正文在内）。索引活性（跨进程重载/带外重建）由各缓存内部自愈；这里复查
    文件存在性，可见性只经 is_retrievable。vec_rels 非空时向量 KNN 命中并入
    候选并集。now 为 None 时不按 valid_until 排除（缺省，lexical_candidates
    不传时钟）；now 提供时 valid_until 已过 ⇒ 检索不可见，get 不受限。
    """
    return [
        mem
        for mem, _stat, _rel in _candidate_stream(
            store, q_tokens, nss, vec_rels, now, project, scored=False
        )
    ]
