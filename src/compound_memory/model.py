"""Domain model shared across modules (moved out of storage to break the cycle)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TypeSpec:
    """一种记忆类型的全套规格——全系统唯一的类型知识源（CONTEXT.md: memory type）。"""

    ttl_days: int | None  # 归档 TTL；None = 永不衰减
    weight: float  # 检索类型权重
    tau_days: float  # 新近半衰期（天）
    # 同 key 冲突检测（P3 谓词的类型维，#46 表驱动化）：True ⇒ 同 ns 同 key
    # 不同内容进 review 队列（key 更新通道）；缺省 False = append-only 安全缺省，
    # 新类型显式加入才走冲突裁决。
    key_conflicts: bool = False


TYPE_SPEC: dict[str, TypeSpec] = {
    "episode": TypeSpec(ttl_days=90, weight=0.5, tau_days=30.0),
    "fact": TypeSpec(ttl_days=None, weight=0.9, tau_days=365.0, key_conflicts=True),
    "insight": TypeSpec(ttl_days=180, weight=0.7, tau_days=90.0, key_conflicts=True),
    "skill": TypeSpec(ttl_days=None, weight=1.0, tau_days=365.0),
    # decision（#46）：长寿如 fact（ttl=None / τ=365）但描述一个已做的选择；
    # 权重与 fact 同档（权威的确定性知识，非可推翻的软性经验）。被新决策取代
    # 走同 key 冲突 → review 裁决（key_conflicts=True），而非衰减归档。
    "decision": TypeSpec(ttl_days=None, weight=0.9, tau_days=365.0, key_conflicts=True),
}
MEMORY_TYPES = tuple(TYPE_SPEC)
TTL_DAYS: dict[str, int | None] = {t: s.ttl_days for t, s in TYPE_SPEC.items()}

# 证据块 recent 明细上限（ADR-0007，perf 守门）：frontmatter parse 是读路径延迟大头，
# 明细封顶防 yaml 膨胀；老明细语义化进计数不丢失，全史审计走 commit 消息（混合三层②）。
EVIDENCE_RECENT_CAP = 10

# 证据块键名单点（ADR-0007/0008）：frontmatter 形态是钉死的 dict（不引入
# dataclass 改形状），此处只收口键名字面量——model 折算/视图、lifecycle 写侧、
# search 展示投影一律引常量，键名变更只动这里。注意展示面（evidence_summary/
# explain 返回 dict 的字段名）是 ADR-0008 的另一层契约，保持字面量以示区分。
EVIDENCE_SUCCESS_COUNT = "success_count"
EVIDENCE_FAILURE_COUNT = "failure_count"
EVIDENCE_CONTRADICTION_COUNT = "contradiction_count"
EVIDENCE_LAST_VERIFIED = "last_verified"
EVIDENCE_RECENT = "recent"


def zero_evidence() -> dict[str, Any]:
    """显式零证据块（ADR-0008）：蒸馏产物是新写记忆，不适用惰性缺省——
    继承源计数同为双重计数，落库时显式 {0,0,0} 起点。每次调用返回新 dict
    （避免共享可变缺省）。"""
    return {
        EVIDENCE_SUCCESS_COUNT: 0,
        EVIDENCE_FAILURE_COUNT: 0,
        EVIDENCE_CONTRADICTION_COUNT: 0,
        EVIDENCE_LAST_VERIFIED: None,
        EVIDENCE_RECENT: [],
    }


def evidence_view(mem: Memory) -> dict[str, Any]:
    """证据块运行时视图（惰性迁移单点，ADR-0007）：无块旧记忆读为
    {success_count: uses, failure_count: 0, contradiction_count: 0}——uses 无差别
    映射 success（诚实反映「只知被用过」），首次 feedback 才落盘写块；无全库改写。
    有块时原样返回 mem.evidence 本体（调用方改完直接赋回，无第二份拷贝）。"""
    if mem.evidence is not None:
        return mem.evidence
    return {
        EVIDENCE_SUCCESS_COUNT: mem.uses,
        EVIDENCE_FAILURE_COUNT: 0,
        EVIDENCE_CONTRADICTION_COUNT: 0,
        EVIDENCE_LAST_VERIFIED: None,
        EVIDENCE_RECENT: [],
    }


@dataclass
class Memory:
    id: str
    ns: str
    type: str
    source: str
    created: str
    content: str
    confidence: float = 0.5
    uses: int = 0
    last_used: str | None = None
    links: list[str] = field(default_factory=list)
    ttl: int | None = None
    key: str | None = None
    validated_by: list[str] = field(default_factory=list)
    archived: bool = False
    # 有效期标注（frontmatter 可选字段，ISO date；write 侧校验格式）：
    # valid_from 仅标注；valid_until 已过 ⇒ 检索/邻居/蒸馏候选默认排除（get 与归档不受影响）
    valid_from: str | None = None
    valid_until: str | None = None
    # 写入来源通道（frontmatter 可选字段）：普通写入不落盘，仅蒸馏产物为 "distillation"
    origin: str | None = None
    # 证据块（ADR-0007/0008，frontmatter 可选字段）：带 outcome 反馈事件的唯一运行时
    # 数据源，置信度由此折算、可升可降。None = 无块（存量旧记忆，读时经 evidence_view
    # 惰性迁移；普通写不落盘，仅蒸馏产物显式零块与首次 feedback 落盘写块）。
    # 绝不进 doc_text（scoring.doc_text = content + key）——feedback 不触发向量重编码。
    evidence: dict[str, Any] | None = None
    # 适用范围标注（frontmatter 可选字段，ADR 0010）：小写 slug（校验单点 validation），
    # 空 = 跨项目通用（全局）。检索适用性轴，与 ns 的可见性/属主轴正交；
    # 纯元数据过滤，绝不进落盘路径（namespaces/<ns>/<type>/<id>.md 不变）
    project: str | None = None
