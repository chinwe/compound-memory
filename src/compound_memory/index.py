"""Index: token→path 的可重建缓存（倒排 + per-doc token 统计）。

不变量在此唯一归属：活动记忆必被索引，归档记忆必不在索引。
缓存文件缺失或损坏 ⇒ 经注入的 scan_pairs 全量重建——降级到慢，绝不报错。
活性是 store 级而非进程级：读路径检测跨进程缓存更新（重载）与带外目录
变更（增量对账，只应用 diff）；手编已有文件的内容不改目录 mtime，那条路走显式 rebuild。
检索文本知识来自 scoring.doc_text（单一定义点）。

tokens.json v3 schema（#41 token stats + ADR 0010 的 project 适用性字段）：
    {"v": 3,
     "index": {<token>: [<rel_path>, ...]},            # 倒排（候选选取）
     "docs":  {<rel_path>: {"tf": {<token>: n}, "len": n,  # per-doc 频表（BM25 免 parse）
                "id"/"ns"/"type"/"source"/"confidence"/"uses"/"created"/
                "last_used"/"valid_until"/"project": ...}}}  # 排序先验 + emit 身份 + 适用性
docs 条目固定键集（null 也落盘）：JSON round-trip 后 dict 相等可作对账 diff
基准；先验入缓存意味着跨进程 feedback（只改 uses/last_used）也走对账更新。
project 入条目是检索快路径 fail-closed 的前提：条目缺该键时 _mem_from_entry
严格取键回退 parse（读真值），缓存条目绝不把项目记忆冒充成全局。
旧版 schema（v1 纯倒排 / v2 无 project）视作死缓存 ⇒ 全量重建（缓存可重建语义）。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Callable, Iterable

from .liveness import dirs_newer_than
from .model import Memory
from .scoring import tokenize, doc_text

CACHE_VERSION = 3


def atomic_write_text(path: Path, text: str) -> None:
    """同目录唯一临时文件 + os.replace 原子替换（#18/#21 共享单点）。

    - 中断/失败时目标要么旧完整要么新完整，不留半写文件；
    - 临时名唯一（mkstemp）：并发写者不会踩掉彼此的 replace 源（固定 .tmp 名
      实测 ENOENT）；读路径的惰性重建不经写锁，缓存写出必须自身并发安全；
    - 权限经 fchmod 对齐 open() 默认（0666 & ~umask）——mkstemp 固定 0600 会
      让新落盘文件整体变严，与 write_text 时代行为不一致；
    - 失败清理临时文件（非 .md 后缀不进扫描视野；单文件 unlink 不受沙箱
      批量删除守卫影响）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            mask = os.umask(0)  # 探测 umask 需 set 两步；写者已串行化，实际 umask 进程内不变
            os.umask(mask)
            os.fchmod(fh.fileno(), 0o666 & ~mask)
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass  # 清理失败不掩盖原异常
        raise


def _empty_cache() -> dict[str, Any]:
    return {"v": CACHE_VERSION, "index": {}, "docs": {}}


# docs 条目里 JSON 原生可表达的标量；YAML 会把未加引号的 ISO 日期解析成
# date/datetime 对象（手编文件常见形态），直接进 json.dumps 会炸整场检索
_JSON_SCALARS = (str, int, float, bool, type(None))


def _entry_date(value: Any) -> str | None:
    """日期字段的条目值：str 直存，其余（YAML date 对象等）归 None。

    与 parse 路径的坏日期语义对齐：age_days 对非 str 抛 TypeError → 消费方
    记中性——条目不冒充有效日期，两条路径的新近/过期判定逐位一致。
    """
    return value if isinstance(value, str) else None


def _entry_scalar(value: Any) -> Any:
    """身份字段的条目值：JSON 标量直存，异类型（如 date）退化为 str——
    只保证缓存可落盘不炸检索，异类型本身已是坏数据，语义交给消费方容错。"""
    return value if isinstance(value, _JSON_SCALARS) else str(value)


def _doc_entry(mem: Memory, tokens: list[str]) -> dict[str, Any]:
    """per-doc 缓存条目：token 频表 + 排序先验/emit 身份（#41）。

    固定键集、null 也落盘——JSON round-trip 后 dict 相等即「该条未变」，
    是 _reconcile 的 diff 基准（跨进程 feedback 只改先验也要能被对账捕获）。
    """
    tf: dict[str, int] = {}
    for tok in tokens:
        tf[tok] = tf.get(tok, 0) + 1
    return {
        "tf": tf,
        "len": len(tokens),
        "id": _entry_scalar(mem.id),
        "ns": _entry_scalar(mem.ns),
        "type": _entry_scalar(mem.type),
        "source": _entry_scalar(mem.source),
        "confidence": _entry_scalar(mem.confidence),
        "uses": _entry_scalar(mem.uses),
        "created": _entry_date(mem.created),
        "last_used": _entry_date(mem.last_used),
        "valid_until": _entry_date(mem.valid_until),
        # ADR 0010：适用性字段进白名单键集——快路径候选视图必须携带 project，
        # 否则带 project 的记忆在缓存路径被当全局（fail-closed 破洞：多放行）
        "project": _entry_scalar(mem.project),
    }


class Index:
    """Deep module: 三个动词 sync / candidates / rebuild，缓存机制全部在实现内。

    调用方无需感知缓存何时加载、何时重建、archived 走哪条路，
    也无需感知缓存是否被其他进程更新过——活性检测在读路径内部完成。
    另有一对批式写通道动词 defer / flush_pending：仅 MemoryStore.batch 使用，
    把逐条落盘收拢成批尾一次。doc_entries 暴露 per-doc token 统计供检索
    候选路径免 parse（#41）。
    """

    def __init__(self, root: Path, scan_pairs: Callable[[], list[tuple[Memory, str]]]) -> None:
        self._root = root
        self._scan_pairs = scan_pairs
        self._dir = root / "index"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "tokens.json"
        self._data: dict[str, Any] | None = None  # None = 未加载
        self._dead = False  # 落盘缓存缺失或损坏，待重建
        self._loaded_stamp: int | None = None  # 缓存文件上次加载时的 mtime_ns
        self._deferred = False  # batch() 期间 sync 只改内存态
        self._dirty = False  # deferred 期间有未落盘的内存态变更

    # ---------- 缓存活性（重建/重载协议在这里，调用方不可见） ----------

    def _read_cache(self) -> dict[str, Any] | None:
        """读取并结构校验缓存文件；缺失/损坏/旧版 schema 返回 None（走重建）。"""
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if (
            not isinstance(data, dict)
            or data.get("v") != CACHE_VERSION
            or not isinstance(data.get("index"), dict)
            or not isinstance(data.get("docs"), dict)
        ):
            return None
        return data

    def _load(self) -> dict[str, Any]:
        if self._data is None:
            data = self._read_cache()
            if data is None:
                self._data = _empty_cache()
                self._dead = True
            else:
                self._data = data
                self._loaded_stamp = self._cache_stamp()
        return self._data

    def _cache_stamp(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _ensure_live(self) -> None:
        """写路径保活：缓存缺失/损坏 ⇒ 重建（随后的增量 upsert 基于活缓存）。"""
        self._load()
        if self._dead or not self._path.exists():
            self.rebuild(self._scan_pairs())

    def _ensure_fresh(self) -> None:
        """读路径三级自愈（原 Q1-A 决策的跨进程扩展）：

        1. 缺失/损坏 ⇒ 全量重建；
        2. 缓存文件 mtime 变了（其他进程写过）⇒ 丢弃内存态重载；
        3. 任一 ns/type 目录 mtime 晚于缓存文件（带外新增/删除 .md）
           ⇒ 增量对账：scan 后只更新 diff 的条目（perf-bench：千条库带外写后首查的大头）。

        已知边界（2026-10-05 审计 P2-1）：mtime 判活存在同 tick 竞态窗口——
        对方进程在与本缓存基线相同的 mtime 粒度内完成写入时漏检，下次带外
        变更或显式 rebuild 才恢复。本地单写者为主的使用形态下窗口可接受，
        无文件锁的跨进程高并发写入不在保证范围（spec：非安全边界）。
        """
        if self._deferred:
            return  # 批内不做读路径自愈：batch 是唯一写者，批尾 flush 后恢复
        self._ensure_live()
        stamp = self._cache_stamp()
        if self._loaded_stamp is not None and stamp != self._loaded_stamp:
            self._data = None
            self._load()
            if self._dead:
                self.rebuild(self._scan_pairs())
                return
            stamp = self._loaded_stamp if self._loaded_stamp is not None else stamp
        if stamp is not None and dirs_newer_than(self._root / "namespaces", stamp):
            self._reconcile()

    def defer(self) -> None:
        """批式写通道入口（仅 MemoryStore.batch 调用）：sync 只改内存态，落盘收拢到 flush_pending。"""
        self._deferred = True
        self._dirty = False

    def flush_pending(self) -> None:
        """批尾一次落盘（无变更则零写入）；恢复读路径活性自愈。"""
        if not self._deferred:
            return
        self._deferred = False
        if self._dirty:
            self._save()
            self._dirty = False

    def _save_lazy(self) -> None:
        """defer 期间只标脏（批尾一次落盘），否则立即落盘。"""
        if self._deferred:
            self._dirty = True
        else:
            self._save()

    def _reconcile(self) -> None:
        """带外增删的增量对账：scan 后只应用 diff（per-doc 条目级比较）。

        与 VectorIndex._reconcile 同构——带外写一条不再放大成全库重建
        （scan + tokenize + tokens.json 全量重写，千条库秒级）。条目相等 =
        tf 频表 + 先验全等（固定键集保证 round-trip 可比）：内容变更、
        跨进程 feedback（只动 uses/last_used）、删除/挪位都各自落到 diff
        分支。diff 应用在内存态完成后一次落盘；缓存与全量重建是集合等价的
        （rels 列表顺序不保证一致，candidates 的 sorted 输出不受影响）。
        """
        data = self._load()
        index, docs = data["index"], data["docs"]
        active: dict[str, dict[str, Any]] = {}
        for mem, rel in self._scan_pairs():
            if not mem.archived:
                active[rel] = _doc_entry(mem, tokenize(doc_text(mem)))
        changed = False
        for rel, entry in active.items():
            if docs.get(rel) == entry:
                continue
            self._purge(rel)
            for tok in entry["tf"]:
                index.setdefault(tok, []).append(rel)
            docs[rel] = entry
            changed = True
        for rel in list(docs):
            if rel not in active:
                self._purge(rel)
                changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        # 目录可能被外部整体移走（测试模拟缓存丢失、或人为 rm -rf index/），写前确保存在
        self._dir.mkdir(parents=True, exist_ok=True)
        # 原子写共享单点（唯一临时名 + fchmod 权限对齐 + 失败清理），语义见函数 docstring
        atomic_write_text(self._path, json.dumps(self._data or {}, ensure_ascii=False, sort_keys=True))
        self._loaded_stamp = self._cache_stamp()  # 自己写盘后刷新基线，避免自触发重载

    # ---------- interface ----------

    def sync(self, mem: Memory, rel_path: str) -> None:
        """使索引与 mem 一致：active ⇒ rel_path 已索引；archived ⇒ rel_path 已移除。

        rel_path 指该记忆在活动区的路径；归档场景由调用方传入原活动路径。
        """
        self._ensure_live()
        if mem.archived:
            self._remove(rel_path)
        else:
            self._upsert(mem, rel_path)

    def candidates(self, tokens: list[str]) -> list[str]:
        """文档含任一 query token 的相对路径；[] 表示无匹配。

        读路径三级自愈（跨进程重载 / 带外重建）在内部完成，调用方无感。
        """
        self._ensure_fresh()
        index = self._load()["index"]
        rels: set[str] = set()
        for tok in tokens:
            rels.update(index.get(tok, []))
        return sorted(rels)

    def doc_entries(self, rels: Iterable[str]) -> dict[str, dict[str, Any]]:
        """按 rel 取缓存的 per-doc 条目（tf 频表 / len / 排序先验，#41）。

        检索候选路径的免 parse 数据源：只有活动区（在索引里）的 rel 才会有
        条目；缺失的 rel（向量路独有召回等）不出现在返回里，调用方回退
        parse。自愈协议与 candidates 同款；条目字段级损坏由调用方容错
        （检索降级不报错），结构级损坏在 _read_cache 拦下走重建。
        """
        self._ensure_fresh()
        docs = self._load()["docs"]
        return {rel: docs[rel] for rel in rels if rel in docs}

    def rebuild(self, memories: list[tuple[Memory, str]]) -> dict[str, int]:
        """以 (memory, rel_path) 序对全量重建；返回计数。

        archived 的序对跳过（不进倒排也不进 docs）：检索读路径经缓存条目
        无从复查 archived 标记，跳过保持「归档不可见」语义与查询侧过滤
        等价（与 _reconcile 的非归档集合一致——两者集合等价不变量）。
        """
        index: dict[str, list[str]] = {}
        docs: dict[str, dict[str, Any]] = {}
        for mem, rel in memories:
            if mem.archived:
                continue
            entry = _doc_entry(mem, tokenize(doc_text(mem)))
            for tok in entry["tf"]:
                index.setdefault(tok, []).append(rel)
            docs[rel] = entry
        self._data = {"v": CACHE_VERSION, "index": index, "docs": docs}
        self._dead = False
        self._save()
        return {"memories": len(memories), "tokens": len(index)}

    # ---------- 内部：变更原语 ----------

    def _purge(self, rel_path: str) -> None:
        """清除某条路径的全部词条与 docs 条目残留（不落盘，由调用方决定后续写）。"""
        data = self._load()
        index = data["index"]
        for tok in list(index):
            if rel_path in index[tok]:
                index[tok].remove(rel_path)
                if not index[tok]:
                    del index[tok]
        data["docs"].pop(rel_path, None)

    def _upsert(self, mem: Memory, rel_path: str) -> None:
        """加入/刷新一条记忆的词条与 docs 条目；先移除其残留旧路径。"""
        self._purge(rel_path)
        data = self._load()
        entry = _doc_entry(mem, tokenize(doc_text(mem)))
        for tok in entry["tf"]:
            data["index"].setdefault(tok, []).append(rel_path)
        data["docs"][rel_path] = entry
        self._save_lazy()

    def _remove(self, rel_path: str) -> None:
        self._purge(rel_path)
        self._save_lazy()
