"""Domain model shared across modules (moved out of storage to break the cycle)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TypeSpec:
    """一种记忆类型的全套规格——全系统唯一的类型知识源（CONTEXT.md: memory type）。"""

    ttl_days: int | None  # 归档 TTL；None = 永不衰减
    weight: float  # 检索类型权重
    tau_days: float  # 新近半衰期（天）


TYPE_SPEC: dict[str, TypeSpec] = {
    "episode": TypeSpec(ttl_days=90, weight=0.5, tau_days=30.0),
    "fact": TypeSpec(ttl_days=None, weight=0.9, tau_days=365.0),
    "insight": TypeSpec(ttl_days=180, weight=0.7, tau_days=90.0),
    "skill": TypeSpec(ttl_days=None, weight=1.0, tau_days=365.0),
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
    # 写入来源通道（frontmatter 可选字段）：普通写入不落盘，仅蒸馏产物为 "distillation"
    origin: str | None = None
