"""存储层：Markdown + YAML frontmatter、命名空间、git、复利引擎。

根目录布局：
    namespaces/<ns>/<type>/<id>.md   活动记忆
    archive/<ns>/<type>/<id>.md      衰减归档（可恢复）
    index/tokens.json                可重建的词法检索缓存
    index/vectors.db                 可重建的向量检索缓存（vec extra，缺失时自动降级）
    review-queue.md                  fact/insight 冲突队列
"""

from __future__ import annotations

import datetime as dt
import dataclasses
import os
import shutil
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, overload

import yaml

from .index import Index
from .model import MEMORY_TYPES, TTL_DAYS, Memory
from .review_queue import ReviewQueue
from .scoring import age_days, doc_text, dup_similarity_matrix, is_expired, rank, recency_age, tokenize
from .vector_index import VectorIndex

# frontmatter 解析 loader：C 扩展（libyaml）快 ~5x 且与 SafeLoader 语义逐位一致
# （perf-bench：scan_pairs 的 yaml parse 是对账/统计读路径的最大单项），
# 无 C 扩展的安装回退纯 Python loader——行为不变，只慢
try:
    from yaml import CSafeLoader as _SafeLoader
except ImportError:  # pragma: no cover - 取决于 PyYAML 是否带 C 扩展
    from yaml import SafeLoader as _SafeLoader  # type: ignore[assignment]

# 向量召回候选池：词面候选 ∪ 向量 KNN 前 VEC_POOL 条（ns/活性过滤后）
VEC_POOL = 16

ARCHIVE_USES_THRESHOLD = 3
CONF_USE_BUMP = 0.1
CONF_CROSS_AGENT_BUMP = 0.15
GIT_IDENTITY = ("-c", "user.name=compound-memory", "-c", "user.email=memory@local")
# 蒸馏信号阈值（distill-plan 单一定义点；CLI --help 文本由这两个常量生成，不会漂移）：
# 疑似重复 = normalized_similarity(BM25/n_query_tokens) 达到该值；晋升建议 = episode 高活性门槛
DISTILL_DUP_SIM_THRESHOLD = 0.5
PROMOTION_USES_THRESHOLD = 5
# stats 健康度桶（#8：固定边界保证跨期可比，恒输出全桶）——
# uses 桶按复利语义划线：0=死本金、3=归档存活线（ARCHIVE_USES_THRESHOLD）、10+=高价值；
# confidence 桶：<0.3 低信、0.3-0.6 写入默认带、0.6-0.8 已验证、0.8+ 高置信
USES_HISTOGRAM_BUCKETS = ("0", "1-2", "3-5", "6-9", "10+")
CONFIDENCE_HISTOGRAM_BUCKETS = ("<0.3", "0.3-0.6", "0.6-0.8", "0.8-1.0")
RECENT_WINDOW_DAYS = 7


def _unlink_file(path: Path) -> None:
    """默认删除 adapter（测试侧经 conftest 注入沙箱安全版本）。"""
    if path.exists():
        path.unlink()


def _git_available() -> bool:
    """默认 git 探测 adapter（测试侧经 git_probe 注入，勿 patch 全局 shutil.which）。"""
    return shutil.which("git") is not None


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


def _check_validity(valid_from: str | None, valid_until: str | None) -> None:
    """有效期字段校验（write 单点）：ISO date 格式 + from<=until，坏输入响亮抛 ValueError。"""
    for name, value in (("valid_from", valid_from), ("valid_until", valid_until)):
        if value is None:
            continue
        try:
            dt.date.fromisoformat(value)
        except (ValueError, TypeError):
            raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD), got: {value!r}")
    if valid_from and valid_until and dt.date.fromisoformat(valid_from) > dt.date.fromisoformat(valid_until):
        raise ValueError(f"valid_from {valid_from!r} is after valid_until {valid_until!r}")


def default_root() -> Path:
    """记忆库根目录解析单一定义点：$COMPOUND_MEMORY_ROOT 优先，否则 ~/.agents/memory。

    CLI 与 MCP server 两个 adapter 都从这里取默认——环境变量名与回退路径不得另写一份。
    """
    env = os.environ.get("COMPOUND_MEMORY_ROOT")
    return Path(env) if env else Path.home() / ".agents" / "memory"


class MemoryStore:
    def __init__(
        self,
        root: Path | str,
        git: bool = True,
        clock: Callable[[], dt.date] = dt.date.today,
        remover: Callable[[Path], None] | None = None,
        git_probe: Callable[[], bool] | None = None,
        embedder: Callable[[list[str]], list[list[float]]] | None = None,
        agent_id: str | None = None,
    ) -> None:
        self.root = Path(root)
        self.ns_root = self.root / "namespaces"
        self.archive_root = self.root / "archive"
        self.index = Index(self.root, scan_pairs=self._scan_pairs)
        self.vector_index = VectorIndex(self.root, scan_pairs=self._scan_pairs, embedder=embedder)
        self._embedder = embedder
        self._review_queue = ReviewQueue(self.root / "review-queue.md", clock=clock)
        self.git_enabled = git and (git_probe or _git_available)()
        self._clock = clock
        self._remover = remover or _unlink_file
        # 进程侧身份证明：agent_id 非空时（宿主经 COMPOUND_MEMORY_AGENT_ID 注入），
        # 所有调用方自报身份（source/reader/agent）必须与其一致，缺省 reader 自动补真值。
        # 只由 server/cli 入口显式传入，store 自身不读环境变量（测试与库调用保持确定性）。
        self.agent_id = agent_id
        self._ensure_layout()
        if self.git_enabled and not (self.root / ".git").exists():
            # init commit 仅限首次创建：__init__ 在每次 CLI/MCP 启动都会执行，
            # 无条件 add -A + commit 会把带外手编的文件吞进误导性的 "init" 提交
            self._git("init", "-q", check=False)
            self._git("add", "-A", check=False)
            self._git("commit", "-qm", "init compound-memory store", check=False)

    def _today(self) -> str:
        return self._clock().isoformat()

    def _new_id(self) -> str:
        return f"{self._clock().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"

    # ---------- 布局 / git ----------

    def _ensure_layout(self) -> None:
        shared = self.ns_root / "_shared"
        for t in MEMORY_TYPES:
            (shared / t).mkdir(parents=True, exist_ok=True)
        self.archive_root.mkdir(parents=True, exist_ok=True)
        # 运行时工件目录清单归这里一处所有（index/ 缓存、distill/ 蒸馏产物）——
        # scripts/distill-prepare.sh 不再自行补写；已存在的旧库缺行时补齐
        gitignore = self.root / ".gitignore"
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        missing = [line for line in ("index/\n", "distill/\n") if line not in existing]
        if missing and existing and not existing.endswith("\n"):
            missing[0] = "\n" + missing[0]  # 手编文件缺尾换行时先补，避免拼接坏行
        if missing:
            with gitignore.open("a", encoding="utf-8") as fh:
                fh.writelines(missing)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *GIT_IDENTITY, *args],
            capture_output=True,
            text=True,
            check=check,
        )

    def _commit(self, message: str) -> None:
        if not self.git_enabled:
            return
        self._git("add", "-A", check=False)
        self._git("commit", "-qm", message, check=False)

    # ---------- 文件 IO ----------

    def _active_path(self, mem: Memory) -> Path:
        return self.ns_root / mem.ns / mem.type / f"{mem.id}.md"

    def _archive_path(self, mem: Memory) -> Path:
        return self.archive_root / mem.ns / mem.type / f"{mem.id}.md"

    def _save(self, mem: Memory) -> None:
        path = self._archive_path(mem) if mem.archived else self._active_path(mem)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {}
        for key, value in asdict(mem).items():
            if key == "content":
                continue
            if value is None or value == "" or value == []:
                continue
            if key in ("uses",) and value == 0:
                continue
            if key == "archived" and not value:
                continue
            meta[key] = value
        body = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n" + mem.content.strip() + "\n"
        path.write_text(body, encoding="utf-8")

    @staticmethod
    def parse(path: Path) -> Memory:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError(f"bad memory file (missing frontmatter): {path}")
        _, fm, body = text.split("---\n", 2)
        meta = yaml.load(fm, Loader=_SafeLoader) or {}
        meta["content"] = body.strip()
        defaults = {
            f.name: f.default
            for f in dataclasses.fields(Memory)
            if f.default is not dataclasses.MISSING and f.name != "content"
        }
        defaults.pop("content", None)
        return Memory(**{**defaults, **{k: v for k, v in meta.items() if k in {f.name for f in dataclasses.fields(Memory)}}})

    def find(self, mem_id: str) -> Memory | None:
        for base in (self.ns_root, self.archive_root):
            for path in base.rglob(f"{mem_id}.md"):
                return self.parse(path)
        return None

    # ---------- 公开接口 ----------
    #
    # 接口错误约定（单一定义，adapter 各翻译一次）：
    # - 调用方错误（参数非法 / 越权写命名空间 / 自链接）⇒ 抛 ValueError / PermissionError；
    # - 目标记忆不存在 ⇒ 正常返回 {"found": False}——所有按 id 的动词恒含 found 键。

    @staticmethod
    def _check_ns(ns: str) -> None:
        """ns 格式校验（write/search 共用）：非法 ns 是调用方错误，必须抛错——
        search 侧静默返回空结果会让 agent 误判"无相关记忆"。"""
        if ns != "_shared" and not ns.startswith("agent-"):
            raise ValueError("ns must be '_shared' or start with 'agent-'")

    @staticmethod
    def _check_ns_owner(ns: str, identity: str | None, role: str = "reader") -> None:
        """读/反馈侧 owner 校验：agent-* 私有 ns 只有属主宿主可读、可反馈。

        与 write 的 `_check_ns + PermissionError` 对称——写侧已保证非属主写不进
        私有 ns，读侧若不校验则任何宿主显式传 ns=agent-<别人> 即可越权读全量
        （2026-10-03 实测：search 签名原本无调用方身份参数，跨宿主零阻力）；
        feedback 侧不校验则外来 agent 可刷 uses/confidence 或复活归档。

        identity 缺省时对 _shared 放行、对 agent-* 拒绝：宁可不读，不猜身份。
        role 只是让报错指引对得上调用方的参数名（reader / agent）。
        """
        if not ns.startswith("agent-"):
            return
        owner = ns[len("agent-"):]
        if identity in (ns, owner):
            return
        raise PermissionError(
            f"namespace {ns!r} is private to {owner!r}; {role} is {identity!r}. "
            f"Pass {role}={ns!r} or {role}={owner!r} if you are that host."
        )

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...

    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...

    def _resolve_identity(self, value: str | None, role: str) -> str | None:
        """身份裁决：进程注入（agent_id）优先于调用方自报。

        - 未启用 attestation（agent_id 为空）⇒ 原样放行，行为同旧版（自报身份）。
        - 调用方缺省 ⇒ 自动补进程身份（诚实缺省，如 search 私有 ns 忘带 reader）。
        - 调用方与进程身份等价（agent-x / x 两种形式）⇒ 归一化为 agent_id，
          保证 validated_by 等记录字段去重一致。
        - 调用方与进程身份矛盾 ⇒ 响亮报错（伪造/配错宿主都该炸，不该静默改写）。
        """
        if self.agent_id is None:
            return value
        if value is None:
            return self.agent_id
        accepted = {self.agent_id, self.agent_id.removeprefix("agent-")}
        if value in accepted:
            return self.agent_id
        raise PermissionError(
            f"{role} {value!r} contradicts attested agent {self.agent_id!r} "
            f"(COMPOUND_MEMORY_AGENT_ID); the process identity wins"
        )

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
    ) -> dict[str, Any]:
        source = self._resolve_identity(source, "source")
        _check_validity(valid_from, valid_until)
        mem, conflict_with = self._write_new(
            content,
            type=type,
            source=source,
            ns=ns,
            key=key,
            links=links,
            created=created,
            confidence=confidence,
            origin=origin,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        self._commit(f"write {mem.id} ({type}/{ns}) by {source}")
        return self._write_result(mem, conflict_with)

    def _write_new(
        self,
        content: str,
        type: str,
        source: str,
        ns: str,
        key: str | None,
        links: list[str] | None,
        created: str | None,
        confidence: float | None,
        origin: str | None,
        valid_from: str | None = None,
        valid_until: str | None = None,
    ) -> tuple[Memory, Memory | None]:
        """write 的无 commit 核心——distill_apply 复用它把产物写入 + 源归档收进一次 commit。"""
        if type not in MEMORY_TYPES:
            raise ValueError(f"type must be one of {MEMORY_TYPES}, got: {type!r}")
        self._check_ns(ns)
        if ns.startswith("agent-") and source not in (ns, ns[len("agent-"):]):
            raise PermissionError(f"namespace {ns!r} is private to its owner; writer is {source!r}")
        conflict_with: Memory | None = None
        if key and type in ("fact", "insight"):
            conflict_with = self._find_by_key(ns, type, key, exclude_content=content)
        mem = Memory(
            id=self._new_id(),
            ns=ns,
            type=type,
            source=source,
            created=created or self._today(),
            content=content,
            confidence=0.5 if confidence is None else confidence,
            links=list(links or []),
            ttl=TTL_DAYS[type],
            key=key,
            origin=origin,
            valid_from=valid_from,
            valid_until=valid_until,
        )
        self._save(mem)
        if conflict_with is not None:
            self._review_queue.append(conflict_with, mem)
        self._sync_indexes(mem, self._active_rel(mem))
        return mem, conflict_with

    @staticmethod
    def _write_result(mem: Memory, conflict_with: Memory | None) -> dict[str, Any]:
        result = asdict(mem)
        result["conflict"] = conflict_with is not None
        if conflict_with is not None:
            result["conflicts_with"] = conflict_with.id
        return result

    def get(self, mem_id: str, include_neighbors: bool = True, reader: str | None = None) -> dict[str, Any]:
        reader = self._resolve_identity(reader, "reader")
        mem = self.find(mem_id)
        if mem is None:
            return {"found": False}
        self._check_ns_owner(mem.ns, reader)
        result = asdict(mem)
        result["found"] = True
        # links 输出同 ns 脱敏：跨 ns 遗留链不把对侧 id 暴露给本侧读者（与邻居召回同规则）
        same_ns_links: list[str] = []
        for l in mem.links:
            t = self.find(l)
            if t is None or t.ns == mem.ns:
                same_ns_links.append(l)
        result["links"] = same_ns_links
        if include_neighbors and mem.links:
            neighbors = [asdict(n) for n in (self.find(l) for l in same_ns_links) if n is not None]
            result["neighbors"] = neighbors
        return result

    def feedback(self, mem_id: str, agent: str) -> dict[str, Any]:
        agent = self._resolve_identity(agent, "agent")
        mem = self.find(mem_id)
        if mem is None:
            return {"found": False}
        # 私有记忆只有属主可反馈：防外来 agent 刷 uses/confidence、混入 validated_by 或复活归档
        self._check_ns_owner(mem.ns, agent, role="agent")
        if mem.archived:
            self._move_to_active(mem)
        mem.uses += 1
        bump = CONF_USE_BUMP
        if agent not in mem.validated_by:
            if agent != mem.source:
                bump += CONF_CROSS_AGENT_BUMP
            mem.validated_by.append(agent)
        mem.confidence = round(min(1.0, mem.confidence + bump), 3)
        mem.last_used = self._today()
        self._save(mem)
        self._sync_indexes(mem, self._active_rel(mem))
        self._commit(f"feedback {mem.id} by {agent}: uses={mem.uses} conf={mem.confidence}")
        result = asdict(mem)
        result["found"] = True
        return result

    def link(self, id_a: str, id_b: str) -> dict[str, Any]:
        if id_a == id_b:
            raise ValueError("cannot link a memory to itself")
        mem_a, mem_b = self.find(id_a), self.find(id_b)
        missing = [mid for mid, m in ((id_a, mem_a), (id_b, mem_b)) if m is None]
        if missing:
            return {"found": False, "missing": missing}
        assert mem_a is not None and mem_b is not None
        # 跨 ns 链会把对侧 id 写进本侧文件 frontmatter，成为私有 id 的泄漏源；
        # 且邻居召回本就同 ns 过滤，跨 ns 链对复利无贡献——创建侧直接禁止
        if mem_a.ns != mem_b.ns:
            raise ValueError(f"cannot link memories across namespaces: {mem_a.ns!r} vs {mem_b.ns!r}")
        if id_b not in mem_a.links:
            mem_a.links.append(id_b)
        if id_a not in mem_b.links:
            mem_b.links.append(id_a)
        self._save(mem_a)
        self._save(mem_b)
        self._commit(f"link {id_a} <-> {id_b}")
        return {"found": True, "a": id_a, "b": id_b, "links": mem_a.links}

    def search(
        self,
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
        if ns is None:
            reader = self._resolve_identity(reader, "reader")
            scopes = ["_shared"]
            if reader:
                private = f"agent-{reader.removeprefix('agent-')}"
                # 私有侧派生自 reader，属主校验恒真但按"读正文先过门"的不变量照走，
                # 防未来派生逻辑变化时静默放行
                self._check_ns_owner(private, reader)
                scopes.append(private)
        else:
            self._check_ns(ns)
            reader = self._resolve_identity(reader, "reader")
            self._check_ns_owner(ns, reader)
            scopes = [ns]
        now = self._clock()
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        vec_sims, vec_rels = self._vector_recall(query, set(scopes), now)
        return rank(
            query,
            self._candidates(q_tokens, set(scopes), vec_rels, now),
            now=now,
            top_k=top_k,
            neighbor_lookup=(lambda mid: self._active_neighbors(mid, set(scopes), now)) if include_neighbors else None,
            vec_sims=vec_sims,
        )

    def _vector_recall(self, query: str, nss: set[str], now: dt.date) -> tuple[dict[str, float] | None, list[str]]:
        """向量召回：查询编码 + KNN（大池取回后按 ns 集合/去重收敛到 VEC_POOL）。

        返回 (vec_sims, vec_rels)；embedder 未注入或任何故障 ⇒ (None, []) 纯词面降级。
        过期记忆与归档同等排除——候选并集两侧同一套活性语义，不给过期记忆留向量旁路。
        """
        if self._embedder is None:
            return None, []
        try:
            qvec = self._embedder([query])[0]
            hits = self.vector_index.knn(qvec, k=64)
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
                path = self.root / rel_path
                if not path.exists():
                    continue
                mem = self.parse(path)
                if mem.archived or mem.ns not in nss or is_expired(mem, now):
                    continue
                sims[mem_id] = cos
                rels.append(rel_path)
            return (sims or None), rels
        except Exception:
            return None, []

    def _active_neighbors(self, mem_id: str, nss: set[str], now: dt.date) -> list[Memory]:
        """邻居召回的数据源：hit 的一度 links，归档/过期邻居不召回（截断/上限/去环归 rank）。

        ns 集合过滤是访问控制的一部分，不可省：_shared 记忆若链到 agent-* 私有记忆，
        邻居会把私有正文带进调用方不可见的检索结果（2026-10-03 实测泄漏）；
        集合由 search 按"调用方可见的 ns"圈定（双通道 = _shared ∪ 自有私有 ns）。
        """
        mem = self.find(mem_id)
        if mem is None:
            return []
        out: list[Memory] = []
        for link_id in mem.links:
            neighbor = self.find(link_id)
            if (
                neighbor is not None
                and not neighbor.archived
                and not is_expired(neighbor, now)
                and neighbor.ns in nss
            ):
                out.append(neighbor)
        return out

    # ---------- 衰减 / 归档 / 复活 ----------

    def decay_sweep(self) -> list[str]:
        now = self._clock()
        archived: list[str] = []
        for path in sorted(self.ns_root.rglob("*.md")):
            mem = self.parse(path)
            if mem.ttl is None:
                continue
            age = recency_age(mem, now)
            if age is None:
                continue  # 坏/缺日期：跳过该条而非崩掉整场扫描（宁可不归档，不因坏数据丢记忆）
            if age > mem.ttl and mem.uses < ARCHIVE_USES_THRESHOLD:
                self._archive(mem)
                archived.append(mem.id)
        if archived:
            self._commit("decay: archive " + ", ".join(archived))
        return archived

    def revive(self, mem_id: str, reader: str | None = None) -> dict[str, Any]:
        reader = self._resolve_identity(reader, "reader")
        mem = self.find(mem_id)
        if mem is None:
            return {"found": False}
        # revive 返回全文，与 get 同属按 id 读路径：私有 ns 仅属主可复活
        self._check_ns_owner(mem.ns, reader)
        if mem.archived:
            self._move_to_active(mem)
            self._save(mem)
            self._commit(f"revive {mem_id}")
        result = asdict(mem)
        result["found"] = True
        return result

    def _archive(self, mem: Memory) -> None:
        src = self._active_path(mem)
        old_rel = src.relative_to(self.root).as_posix()
        mem.archived = True
        self._save(mem)
        self._remover(src)
        self._sync_indexes(mem, old_rel)

    def _move_to_active(self, mem: Memory) -> None:
        src = self._archive_path(mem)
        mem.archived = False
        self._save(mem)
        self._remover(src)
        self._sync_indexes(mem, self._active_rel(mem))

    # ---------- 蒸馏（确定性段；判断/摘要交调用方 Agent，CONTEXT.md: Distillation） ----------

    def distill_plan(
        self,
        window_days: int = 30,
        min_uses: int = 1,
        min_confidence: float = 0.5,
        ns: str = "_shared",
        reader: str | None = None,
    ) -> dict[str, Any]:
        """蒸馏候选扫描：窗口 + 活性门过滤，产出带信号标注的建议清单（只标注不合并）。

        三类信号：merge_with（同 ns 同 type 同 key，强信号）、possible_dup_of
        （BM25 normalized_similarity ≥ DISTILL_DUP_SIM_THRESHOLD，弱信号）、
        promotion_candidate（episode 高活性，晋升建议——判断后置，#6）。
        归档区不参与；过期（valid_until 已过）与坏日期记忆按宁缺勿滥跳过。

        reader：候选带正文返回，扫私有 ns 须属主（与 get/search 同规则）。
        """
        self._check_ns(ns)
        reader = self._resolve_identity(reader, "reader")
        self._check_ns_owner(ns, reader)
        now = self._clock()
        cands: list[Memory] = []
        for path in sorted((self.ns_root / ns).rglob("*.md")):
            mem = self.parse(path)
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
        }

    def distill_apply(
        self,
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
        source = self._resolve_identity(source, "source")
        source_ids = list(dict.fromkeys(source_ids))  # 去重保序：重复源只归档一次
        sources = [self.find(mid) for mid in source_ids]
        missing = [mid for mid, mem in zip(source_ids, sources) if mem is None]
        if missing:
            return {"found": False, "missing": missing}
        # 蒸馏不跨 ns：私有记忆被当源蒸进 _shared 是正文泄漏通道；
        # distill_plan 本就按单 ns 扫描，源与产物同 ns 是既定流程
        foreign_ns = sorted({s.ns for s in sources if s is not None and s.ns != ns})
        if foreign_ns:
            raise ValueError(f"distill sources must live in target ns {ns!r}; found in: {foreign_ns}")
        mem, conflict_with = self._write_new(
            content,
            type=type,
            source=source,
            ns=ns,
            key=key,
            links=source_ids,
            created=None,
            confidence=confidence,
            origin="distillation",
        )
        archived: list[str] = []
        for src in sources:
            assert src is not None
            if not src.archived:
                self._archive(src)
            archived.append(src.id)
        self._commit(f"distill apply {mem.id} <- " + ", ".join(archived))
        result = self._write_result(mem, conflict_with)
        result["found"] = True
        result["archived_sources"] = archived
        return result

    # ---------- 索引（可重建缓存；机制在 index.Index 与 vector_index.VectorIndex） ----------

    def _active_rel(self, mem: Memory) -> str:
        # 统一 POSIX 分隔符：消费端（_candidates 的 ns 前缀剪枝）按 "/" 匹配，
        # Windows 上 str(relative_to) 产出 "\" 会让检索候选被整体剪掉
        return self._active_path(mem).relative_to(self.root).as_posix()

    def _sync_indexes(self, mem: Memory, rel_path: str) -> None:
        """全部写路径的索引收口：词法 + 向量两份缓存一起保活（向量侧 hash 未变时零编码）。"""
        self.index.sync(mem, rel_path)
        self.vector_index.sync(mem, rel_path)

    def _scan_pairs(self) -> list[tuple[Memory, str]]:
        """扫描活动区供 Index 全量重建（注入回调，惰性调用）。"""
        return [
            (self.parse(path), path.relative_to(self.root).as_posix())
            for path in sorted(self.ns_root.rglob("*.md"))
        ]

    def rebuild_index(self) -> dict[str, Any]:
        counts = self.index.rebuild(self._scan_pairs())
        counts.update(self.vector_index.rebuild(self._scan_pairs()))
        return counts

    def _candidates(self, q_tokens: list[str], nss: set[str], vec_rels: list[str] | None = None, now: dt.date | None = None) -> list[Memory]:
        """Indexed lookup: 索引活性（跨进程重载/带外重建）由各缓存内部自愈，
        这里全信索引命中，只逐一复查文件存在性与 ns 集合/活性——防的是索引
        词条与手编文件内容的漂移（改内容不改目录 mtime，那条路走显式 rebuild）。
        vec_rels 非空时，向量 KNN 命中（rel_path 由向量缓存给出）并入候选并集。
        now 提供时同步排除过期记忆（valid_until 已过 ⇒ 检索不可见，get 不受限）。
        ns 前缀剪枝在 parse 之前：活动区 rel 必为 namespaces/<ns>/...（索引不收
        归档），常见词命中近全库的大库上把 ns 过滤提前省掉全部越界 parse。"""
        prefixes = tuple(f"namespaces/{ns}/" for ns in nss)
        rels = list(self.index.candidates(q_tokens))
        for rel_path in vec_rels or []:
            if rel_path not in rels:
                rels.append(rel_path)
        out: list[Memory] = []
        for rel in rels:
            if not rel.startswith(prefixes):
                continue
            path = self.root / rel
            if path.exists():
                mem = self.parse(path)
                if not mem.archived and not (now is not None and is_expired(mem, now)):
                    out.append(mem)
        return out

    # ---------- 冲突 / 统计 ----------

    def _find_by_key(self, ns: str, mtype: str, key: str, exclude_content: str) -> Memory | None:
        base = self.ns_root / ns / mtype
        if not base.exists():
            return None
        for path in sorted(base.rglob("*.md")):
            mem = self.parse(path)
            if mem.key == key and mem.content.strip() != exclude_content.strip():
                return mem
        return None

    def review_queue(self) -> list[str]:
        """冲突队列展示行——行格式的生成与解析都在 ReviewQueue。"""
        return self._review_queue.lines()

    def review_resolve(self, ids: list[str] | None = None, all: bool = False) -> dict[str, Any]:
        """登记冲突已解决：委托 ReviewQueue 清行，resolved>0 时自动 commit。

        裁决（新旧取舍）归调用方——这里只做登记，不做判断（spec 非目标：不自动裁决冲突）。
        """
        out = self._review_queue.resolve(ids=ids, all=all)
        if out["resolved"]:
            self._commit(f"review resolve {out['resolved']} entries")
        return out

    def stats(self) -> dict[str, Any]:
        by_type: dict[str, int] = {}
        by_ns: dict[str, int] = {}
        uses_hist = {bucket: 0 for bucket in USES_HISTOGRAM_BUCKETS}
        conf_hist = {bucket: 0 for bucket in CONFIDENCE_HISTOGRAM_BUCKETS}
        total, archived, conf_sum = 0, 0, 0.0
        recent_feedback, cross_validated = 0, 0
        distilled_total, distilled_recent = 0, 0
        expired_active = 0
        now = self._clock()
        for base, is_archive in ((self.ns_root, False), (self.archive_root, True)):
            for path in base.rglob("*.md"):
                mem = self.parse(path)
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
            "review_queue_entries": len(self.review_queue()),
            "uses_histogram": uses_hist,
            "confidence_histogram": conf_hist,
            "recent_feedback_7d": recent_feedback,
            "cross_validated": cross_validated,
            "distilled_total": distilled_total,
            "distilled_recent_7d": distilled_recent,
            "expired_active": expired_active,
        }

    def git_log(self, limit: int = 5) -> list[str]:
        if not self.git_enabled:
            return []
        proc = self._git("log", "--oneline", f"-{limit}")
        return [line for line in proc.stdout.splitlines() if line.strip()]
