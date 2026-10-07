"""生命周期动词（动词件，#38）：feedback / decay_sweep / revive / forget / archive / move_to_active。

复利引擎写侧（feedback，ADR-0007 证据驱动置信度）+ 衰减归档/复活（decay_sweep/
revive）+ 终态遗忘（forget，ADR-0009/#48）。阈值常量随宿主动词模块（ADR 0003
裁决 6）单一定义于此；包级公开面收窄（#39）后不再 re-export，storage.lifecycle
是唯一导入路径（见 __init__ docstring）。
锁语义逐位保持（红线自查）：feedback/forget/revive 的「find → 门禁 → 改 →
save/sync → commit」整链在 _write_lock 内——find 在锁外时并发 feedback 同一
记忆会读到同一快照、后写覆盖前者，uses/confidence 丢更新（2026-10-05 并发
测试实证）；decay_sweep 的「扫描 + 归档 + 收尾 commit」是单临界区。门禁执行
时序不动（裁决 5）：按 id 动词的 owner 校验在锁内 find 之后（ns 只有
find 后才知道）。回跳 facade 的成员（_archive/_move_to_active 等）一律经
store 实例属性查找——review/distill 的 Deps 与 tests 直呼 store._archive
走的是 facade 薄委托同一缝（打桩缝，#36 沉淀）。
"""

from __future__ import annotations

import datetime as dt
import re
from contextlib import AbstractContextManager
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol, overload

from ..model import (
    EVIDENCE_CONTRADICTION_COUNT,
    EVIDENCE_FAILURE_COUNT,
    EVIDENCE_LAST_VERIFIED,
    EVIDENCE_RECENT,
    EVIDENCE_RECENT_CAP,
    EVIDENCE_SUCCESS_COUNT,
    Memory,
    evidence_view,
)
from ..review_queue import ReviewQueue
from ..scoring import recency_age

# 归档/置信度阈值（lifecycle 单一定义点，storage.lifecycle 是唯一导入路径）：
# ARCHIVE_USES_THRESHOLD 是衰减扫描的归档存活线（uses 达线免归档）；
# CONF_USE_BUMP 每次成功使用分、CONF_CROSS_AGENT_BUMP 新验证者跨宿主加成
# （ADR-0007 折算表：success +0.1，跨宿主首验 +0.15，validated_by 记忆×宿主去重）；
# CONF_FAILURE_PENALTY 负面不对称折算（failure −0.2，重复累计）、
# CONF_FLOOR 数值地板（保持可检索、可复活）；封顶 1.0 沿用。
ARCHIVE_USES_THRESHOLD = 3
CONF_USE_BUMP = 0.1
CONF_CROSS_AGENT_BUMP = 0.15
CONF_FAILURE_PENALTY = 0.2
CONF_FLOOR = 0.05

# feedback outcome 全集（ADR-0007）：未知值是调用方错误（ValueError）；
# success 为缺省——老调用方零破坏。
FEEDBACK_OUTCOMES = ("success", "failure", "contradiction", "obsolete", "unknown")

# forget 的 reason 约束（ADR-0009）：动机短语、单行、限长 80 字符、不贴记忆正文
FORGET_REASON_MAX_CHARS = 80

# reason 进 commit 消息前中性化控制字符（换行/制表会拆散单行审计提交消息，
# NUL 类字符 git 拒收）——与 review_queue._CTRL_CHARS_RE 同理由但不同 artifact
# （那边守队列行格式，这边守提交消息单行），各自随所属格式单点定义
_REASON_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean_reason(reason: str | None) -> str | None:
    """reason 单行化 + 截断（ADR-0009，store 层单点：所有调用方共用同一不变量）。

    控制字符中性化为空格后折叠连续空白；空串/纯空白视同未携带（None），
    消息省略 reason 段。
    """
    if reason is None:
        return None
    text = " ".join(_REASON_CTRL_RE.sub(" ", reason).split())
    return text[:FORGET_REASON_MAX_CHARS] or None


class LifecycleDeps(Protocol):
    """lifecycle 动词实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。

    _resolve_identity 镜像 facade 的 overload 对（同 ReviewDeps 的理由）；
    _archive/_move_to_active 镜像 facade 薄委托签名——feedback/revive/
    decay_sweep 经 store 属性查找回跳，与 review/distill 的 Deps 同缝。
    _review_queue 供 feedback 做冻结判定（ADR-0007：队列存在该记忆的未裁决
    contradiction 行 ⇒ 数值冻结）与行登记。
    """

    root: Path
    ns_root: Path
    _clock: Callable[[], dt.date]
    _remover: Callable[[Path], None]
    _review_queue: ReviewQueue

    def today(self) -> str: ...
    def _write_lock(self) -> AbstractContextManager[None]: ...
    def find(self, mem_id: str) -> Memory | None: ...
    def _check_ns_owner(self, ns: str, identity: str | None, role: str = "reader") -> None: ...
    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]: ...
    def _save(self, mem: Memory) -> None: ...
    def _sync_indexes(self, mem: Memory, rel_path: str) -> None: ...
    def _active_rel(self, mem: Memory) -> str: ...
    def _active_path(self, mem: Memory) -> Path: ...
    def _archive_path(self, mem: Memory) -> Path: ...
    def _archive(self, mem: Memory) -> None: ...
    def _move_to_active(self, mem: Memory) -> None: ...
    def _commit(self, message: str) -> None: ...

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...
    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...
    def _resolve_identity(self, value: str | None, role: str) -> str | None: ...


def _bump_failure_count(ev: dict[str, Any]) -> None:
    """failure_count+1 计数表达式单点：feedback 事件记账（冻结期仅记账、数值
    原地）与 fold_failure 共用这一份，无第二份拷贝。"""
    ev[EVIDENCE_FAILURE_COUNT] = ev.get(EVIDENCE_FAILURE_COUNT, 0) + 1


def fold_failure(mem: Memory) -> None:
    """failure 全额折算单点（feedback 未冻结分支与 review-resolve uphold 共用，
    无第二份拷贝）：conf −0.2 地板 0.05 + failure_count+1（ADR-0007 折算表 +
    裁决「维持」折算）。conf 罚分表达式全仓仅此一份；计数表达式单点在
    _bump_failure_count——feedback 冻结期数值原地不折算，仅经它记账，
    故两侧的折算表达式各自只有一份（冻结门禁把「计数」与「罚分」拆开）。"""
    mem.confidence = round(max(CONF_FLOOR, mem.confidence - CONF_FAILURE_PENALTY), 3)
    ev = evidence_view(mem)
    _bump_failure_count(ev)
    mem.evidence = ev


def _record_event(store: LifecycleDeps, mem: Memory, agent: str, outcome: str) -> dict[str, Any]:
    """feedback 事件记账单点（ADR-0007 明细层）：惰性迁移 + uses/last_used 推进 +
    recent 明细（cap 截断）一次完成——obsolete 与常规两分支共用，无第二份拷贝。
    返回视图供调用方按 outcome 继续原地补计数；mem.evidence 已就位。"""
    ev = evidence_view(mem)  # 惰性迁移先于事件：视图基于事件前的 uses（存量 uses → success_count）
    mem.uses += 1
    today = store.today()
    mem.last_used = today
    ev.setdefault(EVIDENCE_RECENT, []).append({"date": today, "agent": agent, "outcome": outcome})
    ev[EVIDENCE_RECENT] = ev[EVIDENCE_RECENT][-EVIDENCE_RECENT_CAP:]
    mem.evidence = ev
    return ev


def fold_success(mem: Memory, bump: float) -> None:
    """success 折算单点（ADR-0007 折算表 success 行，与 fold_failure 对偶）：
    conf +bump 封顶 1.0。bump 由调用方合成（USE_BUMP + 跨宿主首验加成）——
    验证事实（success_count/last_verified/validated_by）只属于 success，留在
    feedback 事件段；conf 表达式全仓仅此一份。"""
    mem.confidence = round(min(1.0, mem.confidence + bump), 3)


def _feedback_commit_msg(mem: Memory, agent: str, outcome: str) -> str:
    """ADR-0006 审计模板单点：feedback 提交消息——obsolete 与常规两分支共用
    这一份 f-string，勿再造第二份（uses/conf 取提交时终值，调用方在变更后构造）。"""
    return f"feedback {mem.id} by {agent}: outcome={outcome} uses={mem.uses} conf={mem.confidence}"


def feedback(store: LifecycleDeps, mem_id: str, agent: str, outcome: str = "success") -> dict[str, Any]:
    """证据驱动置信度写侧（ADR-0007/0008）：outcome 折算表——success +0.1
    （跨宿主首验 +0.15，validated_by 记忆×宿主去重）、failure −0.2 重复累计
    （地板 0.05 封顶 1.0）、contradiction 数值冻结并登记队列独立行型、obsolete
    无条件立即归档（复活走既有 feedback 自动复活通道）、unknown 仅记事件。
    验证分（validated_by / 跨宿主加成）只属于 success——failure/contradiction
    是负证据或争议，不吃也不发验证分。冻结 = 队列存在该记忆的未裁决
    contradiction 行（ReviewQueue 单点派生，行清掉即解冻）；冻结期事件照记
    （uses/计数/明细/validated_by），数值原地。证据块绝不进 doc_text——
    feedback 内容不变零重编码（test_vector_index 行为钉）。"""
    if outcome not in FEEDBACK_OUTCOMES:
        raise ValueError(f"outcome must be one of {FEEDBACK_OUTCOMES}, got: {outcome!r}")
    agent = store._resolve_identity(agent, "agent")
    # 读-改-写全程临界区：find 在锁外时并发 feedback 同一记忆会读到同一
    # 快照、后写覆盖前者，uses/confidence 丢更新（2026-10-05 并发测试实证）
    with store._write_lock():
        mem = store.find(mem_id)
        if mem is None:
            return {"found": False}
        # 私有记忆只有属主可反馈：防外来 agent 刷 uses/confidence、混入 validated_by 或复活归档
        store._check_ns_owner(mem.ns, agent, role="agent")
        # 冻结判定在本次登记之前：本调用自己登记的行不该冻结自己的事件
        frozen = mem.id in store._review_queue.pending_contradictions()
        if outcome == "obsolete":
            # 无条件立即归档（ADR-0007：不引入「obsolete 但未归档」中间态）；
            # 已归档记忆不复活再归档空转，原位记账
            _record_event(store, mem, agent, outcome)
            if not mem.archived:
                store._archive(mem)  # 含 save（归档区）/ 删活动区 / 索引收口
            else:
                store._save(mem)  # 原位更新 frontmatter 记账，索引无变化
            store._commit(_feedback_commit_msg(mem, agent, outcome))
        else:
            if mem.archived:
                store._move_to_active(mem)
            ev = _record_event(store, mem, agent, outcome)
            new_validator = False
            if outcome == "success":
                ev[EVIDENCE_SUCCESS_COUNT] = ev.get(EVIDENCE_SUCCESS_COUNT, 0) + 1
                ev[EVIDENCE_LAST_VERIFIED] = store.today()
                if agent not in mem.validated_by:
                    new_validator = agent != mem.source
                    mem.validated_by.append(agent)
            elif outcome == "failure" and frozen:
                # 冻结期数值原地、事件照记：仅计数（表达式单点 _bump_failure_count）；
                # 未冻结时计数随 conf 罚分一并由下方 fold_failure 一次完成，勿在此重复累加
                _bump_failure_count(ev)
            elif outcome == "contradiction":
                ev[EVIDENCE_CONTRADICTION_COUNT] = ev.get(EVIDENCE_CONTRADICTION_COUNT, 0) + 1
                store._review_queue.append_contradiction(mem, agent)
            # obsolete/unknown 块内无计数槽位：仅明细 + 提交消息承载（全史走 git）
            if not frozen:
                if outcome == "success":
                    fold_success(mem, CONF_USE_BUMP + (CONF_CROSS_AGENT_BUMP if new_validator else 0))
                elif outcome == "failure":
                    fold_failure(mem)  # 折算单点：conf −0.2 地板 0.05 + failure_count+1
                # contradiction/unknown：数值不动（前者冻结语义本身，后者仅记账）
            store._save(mem)
            store._sync_indexes(mem, store._active_rel(mem))
            store._commit(_feedback_commit_msg(mem, agent, outcome))
    result = asdict(mem)
    result["found"] = True
    return result


def decay_sweep(store: LifecycleDeps) -> list[str]:
    now = store._clock()
    with store._write_lock():  # 批量归档 + 收尾 commit 一个临界区
        archived: list[str] = []
        for mem, _path in store._scan_parsed(store.ns_root):
            if mem.ttl is None:
                continue
            age = recency_age(mem, now)
            if age is None:
                continue  # 坏/缺日期：跳过该条而非崩掉整场扫描（宁可不归档，不因坏数据丢记忆）
            if age > mem.ttl and mem.uses < ARCHIVE_USES_THRESHOLD:
                store._archive(mem)
                archived.append(mem.id)
        if archived:
            store._commit("decay: archive " + ", ".join(archived))
    return archived


def revive(store: LifecycleDeps, mem_id: str, reader: str | None = None) -> dict[str, Any]:
    reader = store._resolve_identity(reader, "reader")
    with store._write_lock():  # 读-改-写全程临界区（同 feedback 的丢更新防御）
        mem = store.find(mem_id)
        if mem is None:
            return {"found": False}
        # revive 返回全文，与 get 同属按 id 读路径：私有 ns 仅属主可复活
        store._check_ns_owner(mem.ns, reader)
        if mem.archived:
            store._move_to_active(mem)
            store._save(mem)
            store._commit(f"revive {mem_id}")
    result = asdict(mem)
    result["found"] = True
    return result


def forget(
    store: LifecycleDeps, mem_id: str, agent: str, reason: str | None = None
) -> dict[str, Any]:
    """终态遗忘（ADR-0009/#48）：文件物理移出 + 单条 forget 提交留痕，内容仅存 git 历史。

    与 archive 的分界：归档是可逆的「暂不检索」（文件还在归档区，feedback
    自动复活是 ADR-0007 有意引用的通道），forget 是「不再被系统携带」的终态
    ——文件彻底移出，get/search/邻居/蒸馏候选天然不可见（文件不存在），全部
    读路径零改动、零过滤；feedback/revive 对被遗忘记忆返回 found: False，
    无复活通道（恢复 = 带外 git 运维 checkout/revert，liveness 把恢复的文件
    当带外新增自愈）。三态模型（活动/归档/不存在）零新增例外：不写墓碑、
    stats 不设 forgotten 计数，遗忘审计走 git log -- <file>。

    锁内序列（写动词读-改-写红线）：find → owner 门禁（role=agent，与
    feedback 同规——防外来 agent 删他人私有记忆）→ 标记 archived 后走
    _sync_indexes 既有收口移出两份索引（Index.sync 见 archived ⇒ 词法移除、
    VectorIndex.sync 见 archived ⇒ 引擎移除；跨进程向量残留由 content-hash
    对账自愈，不另写按 id 移除通道）→ remover 物理移出文件（活动区或归档区）
    → 清该 id 的 review 队列行（幂等，行含正文片段而本体已遗忘）→ 单条 commit
    「forget <id> by <agent>[: reason=<r>]」。links 悬空容忍：指向被遗忘记忆的
    links 不摘除（find→None 容错已覆盖邻居召回与蒸馏候选，死链不输出，
    内部留痕有审计价值）。reason 是动机短语（单行化限 80 字符，经 _clean_reason
    收口），不贴记忆正文（对齐 ADR-0006 消息不含正文的隐私约束）。

    顺序注记（spec #52 轴 c1）：实际锁内顺序（archived 翻转 + _sync_indexes
    索引收口先于 _remover 物理移出）与 ADR-0009 文字序（删文件 → 清行 →
    索引移除 → commit）不同——前者是复用 archive 收口缝（_sync_indexes 见
    archived ⇒ 移出两份索引）的自然结果，终态一致（文件不存在、两份索引无
    此 id、队列行清空），全程 _write_lock 内、无外部可观测差异。

    返回恒含 found 键：命中返回删除前快照（archived 如实反映删除前状态）；
    不存在/已遗忘返回 {"found": False}（幂等）。
    """
    agent = store._resolve_identity(agent, "agent")
    reason_text = _clean_reason(reason)
    with store._write_lock():  # 读-改-写全程临界区（同 feedback 的丢更新防御）
        mem = store.find(mem_id)
        if mem is None:
            return {"found": False}
        # 私有记忆只有属主可遗忘：role=agent 与 feedback 同规
        store._check_ns_owner(mem.ns, agent, role="agent")
        snapshot = asdict(mem)  # 删除前快照（archived 标记只为索引缝入参翻转，不落盘）
        src = store._archive_path(mem) if mem.archived else store._active_path(mem)
        # 索引移除复用归档移出的既有收口（_sync_indexes：活动必入索引、归档必不在索引）
        mem.archived = True
        store._sync_indexes(mem, store._active_rel(mem))
        store._remover(src)
        store._review_queue.clear_for(mem.id)  # 幂等清行：无行即零清除，不报错
        message = f"forget {mem.id} by {agent}"
        if reason_text:
            message += f": reason={reason_text}"
        store._commit(message)
    snapshot["found"] = True
    return snapshot


def archive(store: LifecycleDeps, mem: Memory) -> None:
    src = store._active_path(mem)
    old_rel = src.relative_to(store.root).as_posix()
    mem.archived = True
    store._save(mem)
    store._remover(src)
    store._sync_indexes(mem, old_rel)


def move_to_active(store: LifecycleDeps, mem: Memory) -> None:
    src = store._archive_path(mem)
    mem.archived = False
    store._save(mem)
    store._remover(src)
    store._sync_indexes(mem, store._active_rel(mem))
