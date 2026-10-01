"""TYPE_SPEC（类型规格表）测试：全系统唯一的记忆类型知识源。

加一种记忆类型 = 只改 TYPE_SPEC 一张表；MEMORY_TYPES / TTL_DAYS /
TYPE_WEIGHT / TAU_DAYS 全部派生。测试钉住"派生"而非字面量。
"""

from __future__ import annotations

from compound_memory import scoring
from compound_memory.model import MEMORY_TYPES, TTL_DAYS, TYPE_SPEC


def test_memory_types_derived_from_spec():
    assert MEMORY_TYPES == tuple(TYPE_SPEC)


def test_ttl_days_derived_from_spec():
    assert TTL_DAYS == {t: s.ttl_days for t, s in TYPE_SPEC.items()}


def test_scoring_tables_derived_from_spec():
    assert scoring.TYPE_WEIGHT == {t: s.weight for t, s in TYPE_SPEC.items()}
    assert scoring.TAU_DAYS == {t: s.tau_days for t, s in TYPE_SPEC.items()}
