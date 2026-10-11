"""检索可见性谓词（纯函数 peer，不碰 git / 文件系统 / MCP）。

`is_retrievable` 是 archived、valid_until 已过、ns 集合、project 适用性
四轴的唯一合取。词面候选流、向量召回、邻居召回都调用它，调用点不再
各写一份合取式。本模块不读盘、不排序、不索引：过期日期语义仍在
`scoring.is_expired`，属主门禁仍在 `validation`。
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Set as AbstractSet

from ..model import Memory
from ..scoring import is_expired


def in_project_scope(mem_project: str | None, project: str | None) -> bool:
    """project 适用性轴（ADR 0010）。

    字段为空 = 全局通用，任何读方可见；已标注 = 仅声明同一项目的读方可见。
    读方未声明项目（project=None）⇒ 只见全局（fail-closed：「没声明项目 =
    全局会话」，项目记忆不外溢）。

    只判定适用性。检索过滤的合取入口是 `is_retrievable`——可见性/属主
    （ns 轴）与归档、valid_until 不在这里合并。
    """
    return mem_project is None or mem_project == project


def is_retrievable(
    mem: Memory,
    *,
    nss: AbstractSet[str],
    now: dt.date | None,
    project: str | None,
    include_archived: bool = False,
) -> bool:
    """一条记忆是否进入检索候选或邻居召回。

    四轴同时成立才可见：

    - archived：缺省排除。`include_archived=True` 保留归档记忆——检索面
      （search 的候选与邻居）用缺省，显式寻址/溯源面（get 的邻居）传 True。
    - valid_until 已过：`now` 提供时经 `scoring.is_expired` 排除（当日仍
      有效，次日起不可见；坏日期不冒充已过）。`now is None` 表示调用方
      未供时钟，不按这一轴排除（`candidates` 的 now 缺省语义）。
    - ns 集合：`mem.ns` 必须落在 `nss` 内。空集即全排除。
    - project 适用性：见 `in_project_scope`。

    本谓词不做身份裁决（属主门禁仍是 `validation.check_ns_owner`），
    也不决定 get 按 id 读本体（显式寻址恒可读）。
    """
    if mem.archived and not include_archived:
        return False
    if now is not None and is_expired(mem, now):
        return False
    if mem.ns not in nss:
        return False
    return in_project_scope(mem.project, project)
