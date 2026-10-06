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


def test_decision_spec_is_long_lived_and_conflictable():
    """decision 规格锚点（#46）：长寿如 fact（ttl 与 fact 同为 None）且
    走同 key 冲突通道——长寿但描述一个已做的选择，取代走 review 裁决而非衰减。"""
    spec = TYPE_SPEC["decision"]
    assert spec.ttl_days == TYPE_SPEC["fact"].ttl_days is None
    assert spec.key_conflicts is True


def test_conflict_flag_only_on_key_update_channel_types():
    """同 key 冲突标记只出现在走 key 更新通道的类型上（#46 表驱动化后的类型面）。"""
    conflictable = {t for t, s in TYPE_SPEC.items() if s.key_conflicts}
    assert conflictable == {"fact", "insight", "decision"}
