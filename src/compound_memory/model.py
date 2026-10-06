"""Domain model shared across modules (moved out of storage to break the cycle)."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    # 适用范围标注（frontmatter 可选字段，ADR 0010）：小写 slug（校验单点 validation），
    # 空 = 跨项目通用（全局）。检索适用性轴，与 ns 的可见性/属主轴正交；
    # 纯元数据过滤，绝不进落盘路径（namespaces/<ns>/<type>/<id>.md 不变）
    project: str | None = None
