"""MemoryStore 组合点（facade，ADR 0003 / #36）：机制件装配 + 全部动词方法。

骨架片：storage.py 原样迁入本包，模块代码零改动；后续机制件（paths/files/
gitlayer/locking/validation）逐片外移后，facade 对应方法退化为薄委托；
动词方法体的外移归动词票（#37/#38）。包级布局与旧导入面见 __init__.py。
"""

from __future__ import annotations

import datetime as dt
import dataclasses
import fcntl
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator, overload

import yaml

from ..index import Index, atomic_write_text
from ..model import MEMORY_TYPES, TTL_DAYS, Memory
from ..review_queue import ReviewQueue
from ..scoring import age_days, doc_text, dup_similarity_matrix, is_expired, rank, recency_age, tokenize
from ..vector_index import VectorIndex
from . import paths

# storage 域告警的单点 logger：扫描容错（#20）与向量降级（#19）共用
logger = logging.getLogger(__name__)

# frontmatter 解析 loader：C 扩展（libyaml）快 ~5x 且与 SafeLoader 语义逐位一致
# （perf-bench：scan_pairs 的 yaml parse 是对账/统计读路径的最大单项），
# 无 C 扩展的安装回退纯 Python loader——行为不变，只慢
try:
    from yaml import CSafeLoader as _SafeLoader
except ImportError:  # pragma: no cover - 取决于 PyYAML 是否带 C 扩展
    from yaml import SafeLoader as _SafeLoader  # type: ignore[assignment]

# 向量召回候选池：词面候选 ∪ 向量 KNN 前 VEC_POOL 条（ns/活性过滤后）
VEC_POOL = 16

# ns / mem_id 的路径组件白名单：两者都被直接拼进存储路径或 rglob 模式，
# 来自 LLM/宿主输出，格式不设防时 ns='agent-../../x' 可写出存储根、
# mem_id='*' 可经 rglob 命中库内任意记忆（2026-10-05 审计 P1-1/P2-3）。
# 合法 id（YYYYMMDD_hex6）与现有全部 ns 取值均落在 [A-Za-z0-9_-] 内。
_PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# key 是 fact/insight 同 key 更新的稳定锚点，格式约束在落库单点（_write_new，
# write/batch/distill-apply 共用）：小写字母数字段以短横线连接。原为纯文档约定、
# write 无校验，2026-10-05 单日多会话沉淀出成批日期前缀 key——日期化 key 天然
# 一次性（id 已含日期），等于放弃同 key 更新通道。日期前缀的取舍归文档，这里只守字符集与结构。
_KEY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

ARCHIVE_USES_THRESHOLD = 3
CONF_USE_BUMP = 0.1
CONF_CROSS_AGENT_BUMP = 0.15
GIT_IDENTITY = ("-c", "user.name=compound-memory", "-c", "user.email=memory@local")
# index.lock 瞬时冲突的退避序列（_git_retry）：初试 + 每档一次重试
GIT_LOCK_RETRY_DELAYS = (0.05, 0.2)
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


class _Batch:
    """batch() 的句柄：允许批内覆写提交消息（distill_apply 的溯源消息在产物写入后才凑得齐 id）。"""

    def __init__(self, message: str | None = None) -> None:
        self.message = message


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
        self.ns_root = paths.ns_root(self.root)
        self.archive_root = paths.archive_root(self.root)
        self.index = Index(self.root, scan_pairs=self._scan_pairs)
        self.vector_index = VectorIndex(self.root, scan_pairs=self._scan_pairs, embedder=embedder)
        self._embedder = embedder
        self._review_queue = ReviewQueue(self.root / "review-queue.md", clock=clock)
        self.git_enabled = git and (git_probe or _git_available)()
        # git 仓库发现的天花板（防逃逸）：root 的 .git 无效（损坏/被清空）时
        # git 会跳过它继续向上、借父链最近的真仓库执行 add -A/commit
        # （2026-10-05 实测把父仓库的未提交改动收编走）；ceiling 钉在
        # root.parent，无效 .git 报 not a repository 而非逃逸。root 的
        # .git 有效时发现第一跳即命中，行为不变
        self._git_env = {**os.environ, "GIT_CEILING_DIRECTORIES": os.path.realpath(self.root.parent)}
        self._clock = clock
        self._remover = remover or _unlink_file
        # 进程侧身份证明：agent_id 非空时（宿主经 COMPOUND_MEMORY_AGENT_ID 注入），
        # 所有调用方自报身份（source/reader/agent）必须与其一致，缺省 reader 自动补真值。
        # 只由 server/cli 入口显式传入，store 自身不读环境变量（测试与库调用保持确定性）。
        self.agent_id = agent_id
        self._batch_depth = 0  # batch() 嵌套深度（恒 0 或 1：嵌套 batch 是调用方错误）
        self._batch_ops = 0  # 本批延迟的提交计数（批尾消息与"零操作不提交"判据）
        self._write_lock_depth = 0  # 写锁重入深度：batch 持锁期间批内动词直通
        self._ensure_layout()
        if self.git_enabled and not (self.root / ".git").exists():
            # init commit 仅限首次创建：__init__ 在每次 CLI/MCP 启动都会执行，
            # 无条件 add -A + commit 会把带外手编的文件吞进误导性的 "init" 提交
            self._git("init", "-q", check=False)
            self._git("add", "-A", check=False)
            self._git("commit", "-qm", "init compound-memory store", check=False)
        elif self.git_enabled:
            self._recover_orphan_changes()

    def today(self) -> str:
        """当前日期（ISO，注入 clock 的公开出口）：created 缺省、抽取清单等消费。"""
        return self._clock().isoformat()

    def _new_id(self) -> str:
        return f"{self._clock().strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"

    # ---------- 批式落库通道 ----------

    @contextmanager
    def batch(self, message: str | None = None) -> Iterator[_Batch]:
        """批量落库的正门：逐条校验照走、commit 与索引落盘收拢批尾（灌库/蒸馏用）。

        - 每条 write 照常逐条校验并立即落盘（写穿），校验语义与单条 write 完全一致；
          变化只在提交粒度：批尾一次索引 flush（词法落盘一次、向量一次性批量编码）
          + 一次 git commit。逐条 write 的每条全量重写 tokens.json 是 O(n²) 的来源。
        - 失败语义「落地即已提交」：批内异常时已写入条目照常 flush + commit
          （消息注明 partial）后原样上抛——不存在静默半提交。
        - 批内可见性无承诺：同进程词面读经内存索引可能看到已写入条目，向量路与
          跨进程读要等批尾 flush——需要一致快照的调用方不要在批内检索。
        - 嵌套 batch 是调用方错误（ValueError）；feedback/link 等动词的 commit
          在批内同样延迟（_commit 单点拦截），各动词无需批式特化版本。
        """
        if self._batch_depth > 0:
            raise ValueError("nested batch() is not supported")
        with self._write_lock():  # 全程持锁：批内写穿与批尾 flush+commit 同在临界区
            self._batch_depth += 1
            self._batch_ops = 0
            handle = _Batch(message)
            self.index.defer()
            self.vector_index.defer()
            try:
                yield handle
            except BaseException:
                self._end_batch(handle.message, partial=True)
                raise
            self._end_batch(handle.message, partial=False)

    def _end_batch(self, message: str | None, partial: bool) -> None:
        # 先退出批态再 flush：flush 与收尾 commit 不被延迟拦截
        self._batch_depth -= 1
        self.index.flush_pending()
        self.vector_index.flush_pending()
        if self._batch_ops:
            suffix = " (partial)" if partial else ""
            self._commit((message + suffix) if message else f"batch write {self._batch_ops} entries{suffix}")
            self._batch_ops = 0

    # ---------- 跨进程写锁 ----------

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        """写路径动词的跨进程互斥（#21）：覆盖「文件写出 + 缓存更新 + commit」临界区。

        多宿主并发写同一 root 时，git add -A 会扫进他人刚落盘的变更、commit 撞
        index.lock 报错（2026-10-02 实测）；flock 串行化写者后两者皆消。flock 关联
        open file description，同进程重复加锁会自锁——batch 持锁期间批内动词经
        depth 重入直通。锁不可用的异常环境降级无锁并 warning（宁降级勿死锁）；
        读路径与检索不持锁（索引缓存自身并发安全，见 Index._save 的唯一临时名）。
        """
        if self._write_lock_depth > 0:
            self._write_lock_depth += 1
            try:
                yield
            finally:
                self._write_lock_depth -= 1
            return
        fd: int | None = None
        try:
            fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            logger.warning("write lock unavailable: %s; proceeding unlocked", exc)
            if fd is not None:
                os.close(fd)
                fd = None
        self._write_lock_depth += 1
        try:
            yield
        finally:
            self._write_lock_depth -= 1
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    # ---------- 布局 / git ----------

    def _ensure_layout(self) -> None:
        paths.ensure_layout(self.root)

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self.root), *GIT_IDENTITY, *args],
            capture_output=True,
            text=True,
            check=check,
            env=self._git_env,
        )

    def _git_retry(self, *args: str) -> subprocess.CompletedProcess[str]:
        """index.lock 瞬时冲突的有界重试（check=False 语义不变，只多退避重试）。

        覆盖两类真实竞争：flock 降级窗口内的并发写者、外部 git 进程（手工
        操作/其他工具）。hook 拒绝等永久性错误不含 index.lock 字样，一次即
        返回——重试只该买瞬时冲突，不该烧时间在必然重现的失败上。
        """
        step = self._git(*args, check=False)
        for delay in GIT_LOCK_RETRY_DELAYS:
            if step.returncode == 0 or "index.lock" not in (step.stderr or ""):
                break
            time.sleep(delay)
            step = self._git(*args, check=False)
        return step

    def _recover_orphan_changes(self) -> None:
        """启动对账：上次会话 commit 失败/进程中断留在工作树的孤儿变更，
        收编进一个明确标注的恢复提交——否则它们会被下一个写动词的
        "write ..." 消息错位归因（2026-10-02 实测）。带外手编未提交的
        变更同样会被收编：恢复消息不声称作者，语义上诚实；需要专属提交
        历史的带外变更应在手编流程内自行 commit。
        """
        with self._write_lock():  # status 判定与收编同临界区：锁外判定的 TOCTOU 窗口会漏变更
            status = self._git("status", "--porcelain", check=False)
            if status.returncode != 0 or not status.stdout.strip():
                return  # 坏仓库/干净树零副作用：启动路径宁降级
            self._commit("orphan changes recovered")

    def _commit(self, message: str) -> None:
        if self._batch_depth > 0:
            # 批内延迟：commit 收拢到 batch() 退出时一次性执行（单点拦截，各动词无需批式特化）
            self._batch_ops += 1
            return
        if not self.git_enabled:
            return
        staged = self._git_retry("add", "-A")
        combined = (staged.stdout or "") + (staged.stderr or "")
        if staged.returncode != 0:
            # add 未完成就没有可提交的新内容：commit 只会提交 staged 残留，
            # 消息与新变更错位归因（比审计空洞更误导）——短路放弃，变更留
            # 工作树由启动对账收编。check=False 的失败不得静默："git log 即
            # 审计史"的承诺至少要 stderr 响亮一声。
            if "nothing to commit" not in combined:
                print(f"compound-memory: git add failed: {combined.strip()}", file=sys.stderr)
            return
        committed = self._git_retry("commit", "-qm", message)
        combined = (committed.stdout or "") + (committed.stderr or "")
        # nothing-to-commit 是 git 的正常无操作返回，不算失败
        if committed.returncode != 0 and "nothing to commit" not in combined:
            print(f"compound-memory: git commit failed: {combined.strip()}", file=sys.stderr)

    # ---------- 文件 IO ----------

    def _active_path(self, mem: Memory) -> Path:
        return paths.active_path(self.root, mem)

    def _archive_path(self, mem: Memory) -> Path:
        return paths.archive_path(self.root, mem)

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
        # 原子写出收口到共享单点 atomic_write_text（#18/#21）：中断不留半写、
        # 临时名唯一、失败清理、权限对齐 open() 默认
        atomic_write_text(path, body)

    @staticmethod
    def parse(path: Path) -> Memory:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---\n"):
            raise ValueError(f"bad memory file (missing frontmatter): {path}")
        _, fm, body = text.split("---\n", 2)
        meta = yaml.load(fm, Loader=_SafeLoader) or {}
        # 合法 YAML 但非映射（手编标量/列表）：统一转解析失败（#20 容错面覆盖），
        # 否则下面 meta["content"] 抛 TypeError / meta.items() 抛 AttributeError 逃过捕获
        if not isinstance(meta, dict):
            raise ValueError(f"bad memory file (frontmatter not a mapping): {path}")
        meta["content"] = body.strip()
        defaults = {
            f.name: f.default
            for f in dataclasses.fields(Memory)
            if f.default is not dataclasses.MISSING and f.name != "content"
        }
        defaults.pop("content", None)
        # 缺必填字段（手编文件最常见坏法）转译为 ValueError：统一解析失败面，
        # 扫描容错与调用方无需各自特判 TypeError
        try:
            return Memory(**{**defaults, **{k: v for k, v in meta.items() if k in {f.name for f in dataclasses.fields(Memory)}}})
        except TypeError as exc:
            raise ValueError(f"bad memory file (missing required field): {path}: {exc}") from exc

    def _parse_for_scan(self, path: Path) -> Memory | None:
        """扫描路径的容错解析（#20）：坏文件跳过并告警，不炸整场扫描。

        只捕解析类异常（缺 frontmatter / 坏 YAML / 非映射 / 缺必填字段 /
        编码与读盘错误），其他异常照常传播。返回 None 表示跳过；
        文件本身不动，留给人工处置。全好文件零日志，告警即坏信号。
        """
        try:
            return self.parse(path)
        except (ValueError, OSError, yaml.YAMLError) as exc:
            logger.warning("skipping unparseable memory file %s: %s", path, exc)
            return None

    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]:
        """按目录扫描 *.md 并容错解析（#20 扫描消费方共用单点）。

        逐条告警之外，结束时对跳过数量做一次汇总告警（#20 验收：
        数量 + 逐条路径 + 原因，两层都有）。"""
        skipped = 0
        for path in sorted(base.rglob("*.md")):
            mem = self._parse_for_scan(path)
            if mem is None:
                skipped += 1
                continue
            yield mem, path
        if skipped:
            logger.warning("scan skipped %d unparseable memory file(s)", skipped)

    def find(self, mem_id: str) -> Memory | None:
        # mem_id 拼 rglob 模式：非法字符（glob 元字符/路径分隔）不得进入——
        # '*' 曾命中库内任意第一条且绕过属主检查直泄私有正文（审计 P2-3）。
        # 非法 id 语义等价于「不可能存在」⇒ 返回 None：全部调用方对 None
        # 已有容错分支，抛错反而会炸掉邻居召回的「宁缺勿炸」降级。
        if not _PATH_COMPONENT_RE.match(mem_id):
            return None
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
        search 侧静默返回空结果会让 agent 误判"无相关记忆"。

        字符集白名单先行于前缀检查：ns 直接拼进存储路径（_active_path），
        "agent-../../x" 曾可把 .md 写出存储根（2026-10-05 审计 P1-1）——
        白名单同时封死穿越、glob 元字符与路径分隔，且必须在越权检查之前
        （恶意 ns 自证身份的 owner 校验没有资格先跑）。
        """
        if not _PATH_COMPONENT_RE.match(ns):
            raise ValueError(f"ns contains characters outside [A-Za-z0-9_-]: {ns!r}")
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
        with self._write_lock():
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
        """write 的落库核心（无 commit）：commit 由调用方动词收口——单条走 write，
        批式经 batch()（_commit 单点拦截）。tests 亦用它播种 write 会正当拒绝的
        外部 ns fixture（显式字段落库的测试种子）。"""
        if type not in MEMORY_TYPES:
            raise ValueError(f"type must be one of {MEMORY_TYPES}, got: {type!r}")
        if key and not _KEY_RE.match(key):
            raise ValueError(
                f"key must match {_KEY_RE.pattern} (lowercase alphanumeric segments joined by dashes), got: {key!r}"
            )
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
            created=created or self.today(),
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
        # 读-改-写全程临界区：find 在锁外时并发 feedback 同一记忆会读到同一
        # 快照、后写覆盖前者，uses/confidence 丢更新（2026-10-05 并发测试实证）
        with self._write_lock():
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
            mem.last_used = self.today()
            self._save(mem)
            self._sync_indexes(mem, self._active_rel(mem))
            self._commit(f"feedback {mem.id} by {agent}: uses={mem.uses} conf={mem.confidence}")
        result = asdict(mem)
        result["found"] = True
        return result

    def link(self, id_a: str, id_b: str, agent: str | None = None) -> dict[str, Any]:
        """双向关联两条记忆（复利来源②）。跨 ns 禁止（ValueError）；同 ns 私有记忆
        仅属主可连（agent 参数，与 feedback 同规——D1/#34：link 是最后一个无身份
        写入口，按 id 动词在锁内 find 后过门）；_shared 无需身份。"""
        agent = self._resolve_identity(agent, "agent")
        if id_a == id_b:
            raise ValueError("cannot link a memory to itself")
        with self._write_lock():  # 读-改-写全程临界区（同 feedback 的丢更新防御）
            mem_a, mem_b = self.find(id_a), self.find(id_b)
            missing = [mid for mid, m in ((id_a, mem_a), (id_b, mem_b)) if m is None]
            if missing:
                return {"found": False, "missing": missing}
            assert mem_a is not None and mem_b is not None
            # 跨 ns 链会把对侧 id 写进本侧文件 frontmatter，成为私有 id 的泄漏源；
            # 且邻居召回本就同 ns 过滤，跨 ns 链对复利无贡献——创建侧直接禁止
            if mem_a.ns != mem_b.ns:
                raise ValueError(f"cannot link memories across namespaces: {mem_a.ns!r} vs {mem_b.ns!r}")
            # D1 属主门禁：跨 ns 已在上面拒绝，此处两侧同 ns——私有 ns 的 link
            # 写入 frontmatter 同属私有数据变更，仅属主可做（fail-closed，缺身份即拒）
            self._check_ns_owner(mem_a.ns, agent, role="agent")
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
        if top_k < 0:
            raise ValueError(f"top_k must be >= 0, got {top_k}")
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
        except Exception as exc:
            # 宁缺勿炸的降级语义不变；但故障必须可观测（#19）——否则索引损坏/
            # 模型异常会长期被掩盖在「正常降级」里。embedder 未注入是配置路径，
            # 不经此处，不产生日志。
            logger.warning("vector recall degraded to lexical: %s", exc)
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
        with self._write_lock():  # 批量归档 + 收尾 commit 一个临界区
            archived: list[str] = []
            for mem, _path in self._scan_parsed(self.ns_root):
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
        with self._write_lock():  # 读-改-写全程临界区（同 feedback 的丢更新防御）
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

        主候选三类信号：merge_with（同 ns 同 type 同 key，强信号）、possible_dup_of
        （BM25 normalized_similarity ≥ DISTILL_DUP_SIM_THRESHOLD，弱信号）、
        promotion_candidate（episode 高活性，晋升建议——判断后置，#6）。
        另有 key_duplicates 专项段：同 ns 同 type 同 key 组员 ≥2 的多版本组，
        不受窗口/活性门限制（废置旧版 uses=0 进不了主候选，运维实测盲区）。
        归档区不参与；过期（valid_until 已过）与坏日期记忆按宁缺勿滥跳过。

        reader：候选带正文返回，扫私有 ns 须属主（与 get/search 同规则）。
        """
        self._check_ns(ns)
        reader = self._resolve_identity(reader, "reader")
        self._check_ns_owner(ns, reader)
        now = self._clock()
        cands: list[Memory] = []
        for mem, _path in self._scan_parsed(self.ns_root / ns):
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
        for mem, _path in self._scan_parsed(self.ns_root / ns):
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
        with self.batch() as batch_ctx:
            # 源读取在写锁内（batch 持锁）：find 在锁外时，间隙内并发 feedback
            # 的 uses/confidence 会被旧快照在归档写回时覆盖丢失（写动词
            # 读-改-写全程持锁的自查条款）。检查失败零操作，批尾不产生提交。
            sources = [self.find(mid) for mid in source_ids]
            missing = [mid for mid, mem in zip(source_ids, sources) if mem is None]
            if missing:
                return {"found": False, "missing": missing}
            # 蒸馏不跨 ns：私有记忆被当源蒸进 _shared 是正文泄漏通道；
            # distill_plan 本就按单 ns 扫描，源与产物同 ns 是既定流程
            foreign_ns = sorted({s.ns for s in sources if s is not None and s.ns != ns})
            if foreign_ns:
                raise ValueError(f"distill sources must live in target ns {ns!r}; found in: {foreign_ns}")
            result = self.write(
                content,
                type=type,
                source=source,
                ns=ns,
                key=key,
                links=source_ids,
                confidence=confidence,
                origin="distillation",
            )
            archived: list[str] = []
            for src in sources:
                assert src is not None
                if not src.archived:
                    self._archive(src)
                archived.append(src.id)
            batch_ctx.message = f"distill apply {result['id']} <- " + ", ".join(archived)
        result["found"] = True
        result["archived_sources"] = archived
        return result

    # ---------- 索引（可重建缓存；机制在 index.Index 与 vector_index.VectorIndex） ----------

    def _active_rel(self, mem: Memory) -> str:
        return paths.active_rel(self.root, mem)

    def _sync_indexes(self, mem: Memory, rel_path: str) -> None:
        """全部写路径的索引收口：词法 + 向量两份缓存一起保活（向量侧 hash 未变时零编码）。"""
        self.index.sync(mem, rel_path)
        self.vector_index.sync(mem, rel_path)

    def _scan_pairs(self) -> list[tuple[Memory, str]]:
        """扫描活动区供 Index 全量重建（注入回调，惰性调用）。"""
        return [
            (mem, path.relative_to(self.root).as_posix())
            for mem, path in self._scan_parsed(self.ns_root)
        ]

    def rebuild_index(self) -> dict[str, Any]:
        counts = self.index.rebuild(self._scan_pairs())
        counts.update(self.vector_index.rebuild(self._scan_pairs()))
        return counts

    def lexical_candidates(
        self, q_tokens: list[str], nss: set[str], reader: str | None = None
    ) -> list[Memory]:
        """公开的词面候选通道：按 query token 取索引命中的活动记忆（正文在内）。

        凡返回记忆正文的新入口都过身份门：agent-* 必须属主（与 search/get 同一
        规则），_shared 无需身份。extraction 的复述标注（_dup_of）与未来的批量
        复述检测走此正门，勿直取 _candidates 私有件。
        """
        reader = self._resolve_identity(reader, "reader")
        for ns in nss:
            self._check_ns(ns)
            self._check_ns_owner(ns, reader)
        return self._candidates(q_tokens, nss)

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
        for mem, _path in self._scan_parsed(base):
            if mem.key == key and mem.content.strip() != exclude_content.strip():
                return mem
        return None

    def review_queue(self) -> list[str]:
        """冲突队列展示行——行格式的生成与解析都在 ReviewQueue。"""
        return self._review_queue.lines()

    def review_resolve(
        self, ids: list[str] | None = None, all: bool = False, reader: str | None = None
    ) -> dict[str, Any]:
        """登记冲突已解决：委托 ReviewQueue 清行，resolved>0 时自动 commit。

        裁决（新旧取舍）归调用方——按 ids 清行时，传入 id 即裁决的废置方，
        清行同时把该条归档（对侧保留活动区）；--all 只清行，不携带裁决信息，
        不自动归档。spec 非目标：不自动裁决冲突——归档跟随调用方指认的废置
        方，不做方向推断。归档必须跟随清行动作的教训（2026-10-05 运维）：
        清行不归档时废置旧版（uses=0）滞留活动区，且永不出现在 uses≥1 门槛
        的蒸馏候选里——同 key 多版本并存由此累积。

        D2（#34，2026-10-06 决议）：清行按属主可见性收口——agent-* 私有 ns
        的行仅属主可 resolve（可选 reader，与 get/search 同规）；--all 对不可
        见行静默保留（过滤），显式点名他人私有行 PermissionError 原子拒绝；
        _shared 行不受影响。review-queue 展示维持全量（张力见 spec：CLI 是
        本机信任边界，队列行含 content[:40] 片段，MCP 5 tool 不暴露队列）。
        """
        reader = self._resolve_identity(reader, "reader")

        def may_clear(ns: str) -> bool:
            # 行级清行许可（D2）：_shared 恒可清；agent-* 行按属主可见性
            # （fail-closed，身份未知视为不可清）。属主判定单点在
            # _check_ns_owner，这里只包装成谓词供 ReviewQueue 逐行调用。
            if not ns.startswith("agent-"):
                return True
            try:
                self._check_ns_owner(ns, reader)
            except PermissionError:
                return False
            return True

        with self._write_lock():  # 队列文件改写 + 归档 + 登记提交一个临界区
            out = self._review_queue.resolve(ids=ids, all=all, may_clear=may_clear)
            if all:
                out.pop("rows")  # --all 无废置信息，rows 不进返回（CLI 输出同理）
            archived: list[str] = []
            if not all and ids:
                wanted = set(ids)
                for row in out["rows"]:
                    for mem_id in (row["old"], row["new"]):
                        if mem_id not in wanted or mem_id in archived:
                            continue
                        mem = self.find(mem_id)
                        if mem is None or mem.archived:
                            continue
                        self._archive(mem)
                        archived.append(mem_id)
            if out["resolved"]:
                message = f"review resolve {out['resolved']} entries"
                if archived:
                    message += " (archived: " + ", ".join(archived) + ")"
                self._commit(message)
            out["archived"] = archived
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
            for mem, _path in self._scan_parsed(base):
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
        proc = self._git("log", "--oneline", f"-{limit}", check=True)
        return [line for line in proc.stdout.splitlines() if line.strip()]
