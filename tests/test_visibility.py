"""is_retrievable 表驱动单元测试。

检索可见性的四轴（archived / valid_until 已过 / ns 集合 / project 适用性）
只在 visibility.is_retrievable 一处合取。表里每一行只翻动该行要钉的轴，
其余保持「可见」基线，避免合取被一条快乐路径掩盖。
"""

from __future__ import annotations

import datetime as dt

import pytest

from compound_memory.model import Memory
from compound_memory.storage.visibility import is_retrievable

TODAY = dt.date(2026, 10, 1)
YESTERDAY = "2026-09-30"
NSS = frozenset({"_shared", "agent-zcode"})


def _mem(**overrides: object) -> Memory:
    fields: dict[str, object] = {
        "id": "20260101_000001",
        "ns": "_shared",
        "type": "fact",
        "source": "agent-a",
        "created": "2026-01-01",
        "content": "body",
    }
    fields.update(overrides)
    return Memory(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("archived", "ns", "valid_until", "now", "mem_project", "project", "nss", "include_archived", "expected"),
    [
        pytest.param(
            False, "_shared", None, TODAY, None, None, NSS, False, True,
            id="active-global-in-ns",
        ),
        pytest.param(
            True, "_shared", None, TODAY, None, None, NSS, False, False,
            id="archived-excluded-by-default",
        ),
        pytest.param(
            True, "_shared", None, TODAY, None, None, NSS, True, True,
            id="archived-kept-when-requested",
        ),
        pytest.param(
            False, "_shared", None, TODAY, None, None, NSS, True, True,
            id="include-archived-does-not-drop-live",
        ),
        pytest.param(
            False, "_shared", YESTERDAY, TODAY, None, None, NSS, False, False,
            id="valid-until-yesterday-excluded",
        ),
        pytest.param(
            False, "_shared", TODAY.isoformat(), TODAY, None, None, NSS, False, True,
            id="valid-until-today-still-visible",
        ),
        pytest.param(
            False, "_shared", "2027-01-01", TODAY, None, None, NSS, False, True,
            id="future-valid-until-visible",
        ),
        pytest.param(
            False, "_shared", "not-a-date", TODAY, None, None, NSS, False, True,
            id="bad-valid-until-not-treated-as-past",
        ),
        pytest.param(
            False, "_shared", None, TODAY, None, None, NSS, False, True,
            id="missing-valid-until-visible",
        ),
        pytest.param(
            False, "_shared", YESTERDAY, None, None, None, NSS, False, True,
            id="no-clock-skips-valid-until-axis",
        ),
        pytest.param(
            False, "agent-other", None, TODAY, None, None, NSS, False, False,
            id="ns-outside-set-excluded",
        ),
        pytest.param(
            False, "agent-zcode", None, TODAY, None, None, NSS, False, True,
            id="ns-inside-set-visible",
        ),
        pytest.param(
            False, "_shared", None, TODAY, None, None, frozenset(), False, False,
            id="empty-ns-set-excludes-all",
        ),
        pytest.param(
            False, "_shared", None, TODAY, "agenthub", None, NSS, False, False,
            id="project-memory-hidden-from-undeclared-reader",
        ),
        pytest.param(
            False, "_shared", None, TODAY, "agenthub", "agenthub", NSS, False, True,
            id="project-memory-visible-to-same-project",
        ),
        pytest.param(
            False, "_shared", None, TODAY, "agenthub", "webmail", NSS, False, False,
            id="project-memory-hidden-from-other-project",
        ),
        pytest.param(
            False, "_shared", None, TODAY, None, "agenthub", NSS, False, True,
            id="global-memory-visible-to-project-reader",
        ),
        pytest.param(
            True, "_shared", YESTERDAY, TODAY, None, None, NSS, True, False,
            id="include-archived-does-not-waive-valid-until",
        ),
        pytest.param(
            True, "agent-other", None, TODAY, None, None, NSS, True, False,
            id="include-archived-does-not-waive-ns",
        ),
        pytest.param(
            True, "_shared", None, TODAY, "agenthub", None, NSS, True, False,
            id="include-archived-does-not-waive-project",
        ),
    ],
)
def test_is_retrievable_table(
    archived: bool,
    ns: str,
    valid_until: str | None,
    now: dt.date | None,
    mem_project: str | None,
    project: str | None,
    nss: frozenset[str],
    include_archived: bool,
    expected: bool,
) -> None:
    mem = _mem(archived=archived, ns=ns, valid_until=valid_until, project=mem_project)
    assert is_retrievable(
        mem, nss=nss, now=now, project=project, include_archived=include_archived
    ) is expected


def test_include_archived_defaults_to_false() -> None:
    """缺省参数即检索面：归档记忆不进候选，调用方要溯源必须显式打开。"""
    mem = _mem(archived=True)
    assert is_retrievable(mem, nss=NSS, now=TODAY, project=None) is False
