"""检索动词（动词件，#37）：search / vector_recall / active_neighbors /
candidates / lexical_candidates + VEC_POOL。

检索 = 选候选（store 的 layout 职责）+ 排序（scoring.rank 单一定义点），
结果形状逐位不变（契约 test_search 钉住）。门禁执行时序不动：search 是
参数型动词，ns 校验在函数入口（ADR 0003 裁决 5）。VEC_POOL 随宿主动词
（裁决 6），__init__ re-export 保旧导入名。
"""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any, Callable, Protocol, overload

from ..index import Index
from ..model import Memory
from ..scoring import is_expired, rank, tokenize
from ..vector_index import VectorIndex

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
) -> list[dict[str, Any]]:
    """检索 = 选候选（store 的 layout 职责）+ 排序（scoring.rank 单一定义点）。

    embedder 可用时叠加向量召回：候选 = 词面命中 ∪ 向量 KNN（ns/活性过滤），
    两路 rank 在 rank 内 RRF 融合；向量路任何故障都降级纯词面（宁缺勿炸）。

    ns=None（缺省）为双通道检索：_shared ∪ 调用方自有私有 ns（agent-<reader>，
    身份已知时）——私有条目天然出现在结果里，保障不依赖调用方记得显式补搜
    （无身份时退化为单 _shared，与旧版缺省一致）；显式传 ns 保持单 ns 精确语义。
    reader 是调用方身份，ns=agent-* 时必填且须为属主（读侧 owner 校验，
    与 write 的越权抛 PermissionError 对称）。
    """
    if top_k < 0:
        raise ValueError(f"top_k must be >= 0, got: {top_k}")
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
    vec_sims, vec_rels = vector_recall(store, query, set(scopes), now)
    return rank(
        query,
        candidates(store, q_tokens, set(scopes), vec_rels, now),
        now=now,
        top_k=top_k,
        neighbor_lookup=(lambda mid: active_neighbors(store, mid, set(scopes), now)) if include_neighbors else None,
        vec_sims=vec_sims,
    )


def vector_recall(
    store: SearchDeps, query: str, nss: set[str], now: dt.date
) -> tuple[dict[str, float] | None, list[str]]:
    """向量召回：查询编码 + KNN（大池取回后按 ns 集合/去重收敛到 VEC_POOL）。

    返回 (vec_sims, vec_rels)；embedder 未注入或任何故障 ⇒ (None, []) 纯词面降级。
    过期记忆与归档同等排除——候选并集两侧同一套活性语义，不给过期记忆留向量旁路。
    """
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
            sims[mem_id] = cos
            rels.append(rel_path)
        return (sims or None), rels
    except Exception as exc:
        # 宁缺勿炸的降级语义不变；但故障必须可观测（#19）——否则索引损坏/
        # 模型异常会长期被掩盖在「正常降级」里。embedder 未注入是配置路径，
        # 不经此处，不产生日志。
        logger.warning("vector recall degraded to lexical: %s", exc)
        return None, []


def active_neighbors(store: SearchDeps, mem_id: str, nss: set[str], now: dt.date) -> list[Memory]:
    """邻居召回的数据源：hit 的一度 links，归档/过期邻居不召回（截断/上限/去环归 rank）。

    ns 集合过滤是访问控制的一部分，不可省：_shared 记忆若链到 agent-* 私有记忆，
    邻居会把私有正文带进调用方不可见的检索结果（2026-10-03 实测泄漏）；
    集合由 search 按"调用方可见的 ns"圈定（双通道 = _shared ∪ 自有私有 ns）。
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
        ):
            out.append(neighbor)
    return out


def lexical_candidates(
    store: SearchDeps, q_tokens: list[str], nss: set[str], reader: str | None = None
) -> list[Memory]:
    """公开的词面候选通道：按 query token 取索引命中的活动记忆（正文在内）。

    凡返回记忆正文的新入口都过身份门：agent-* 必须属主（与 search/get 同一
    规则），_shared 无需身份。extraction 的复述标注（_dup_of）与未来的批量
    复述检测走此正门，勿直取 candidates 私有件。
    """
    reader = store._resolve_identity(reader, "reader")
    for ns in nss:
        store._check_ns(ns)
        store._check_ns_owner(ns, reader)
    return candidates(store, q_tokens, nss)


def candidates(
    store: SearchDeps,
    q_tokens: list[str],
    nss: set[str],
    vec_rels: list[str] | None = None,
    now: dt.date | None = None,
) -> list[Memory]:
    """Indexed lookup: 索引活性（跨进程重载/带外重建）由各缓存内部自愈，
    这里全信索引命中，只逐一复查文件存在性与 ns 集合/活性——防的是索引
    词条与手编文件内容的漂移（改内容不改目录 mtime，那条路走显式 rebuild）。
    vec_rels 非空时，向量 KNN 命中（rel_path 由向量缓存给出）并入候选并集。
    now 提供时同步排除过期记忆（valid_until 已过 ⇒ 检索不可见，get 不受限）。
    ns 前缀剪枝在 parse 之前：活动区 rel 必为 namespaces/<ns>/...（索引不收
    归档），常见词命中近全库的大库上把 ns 过滤提前省掉全部越界 parse。"""
    prefixes = tuple(f"namespaces/{ns}/" for ns in nss)
    rels = list(store.index.candidates(q_tokens))
    for rel_path in vec_rels or []:
        if rel_path not in rels:
            rels.append(rel_path)
    out: list[Memory] = []
    for rel in rels:
        if not rel.startswith(prefixes):
            continue
        path = store.root / rel
        if path.exists():
            mem = store.parse(path)
            if not mem.archived and not (now is not None and is_expired(mem, now)):
                out.append(mem)
    return out
