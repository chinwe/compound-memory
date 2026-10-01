"""Domain model shared across modules (moved out of storage to break the cycle)."""

from __future__ import annotations

from dataclasses import dataclass, field


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
