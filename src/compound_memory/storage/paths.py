"""布局与路径推导（机制件，#36 片 b）：根目录解析、目录初始化、活动/归档路径。

单一定义点：namespaces/ 与 archive/ 的子目录名只在本模块出现——facade 的
ns_root/archive_root 属性与按 Memory 拼路径一律经这里的模块函数推导。
"""

from __future__ import annotations

import os
from pathlib import Path

from ..model import MEMORY_TYPES, Memory


def default_root() -> Path:
    """记忆库根目录解析单一定义点：$COMPOUND_MEMORY_ROOT 优先，否则 ~/.agents/memory。

    CLI 与 MCP server 两个 adapter 都从这里取默认——环境变量名与回退路径不得另写一份。
    """
    env = os.environ.get("COMPOUND_MEMORY_ROOT")
    return Path(env) if env else Path.home() / ".agents" / "memory"


def ns_root(root: Path) -> Path:
    """活动记忆区根（namespaces/）。"""
    return root / "namespaces"


def archive_root(root: Path) -> Path:
    """衰减归档区根（archive/）。"""
    return root / "archive"


def ensure_layout(root: Path) -> None:
    """初始化目录布局与 .gitignore 工件清单（构造路径调用，幂等）。"""
    shared = ns_root(root) / "_shared"
    for t in MEMORY_TYPES:
        (shared / t).mkdir(parents=True, exist_ok=True)
    archive_root(root).mkdir(parents=True, exist_ok=True)
    # 运行时工件目录清单归这里一处所有（index/ 缓存、distill/ 蒸馏产物、
    # extract/ 抽取清单、.lock 写锁——均不入审计史）——
    # scripts/distill-prepare.sh 不再自行补写；已存在的旧库缺行时补齐
    gitignore = root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    missing = [line for line in ("index/\n", "distill/\n", "extract/\n", ".lock\n") if line not in existing]
    if missing and existing and not existing.endswith("\n"):
        missing[0] = "\n" + missing[0]  # 手编文件缺尾换行时先补，避免拼接坏行
    if missing:
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.writelines(missing)


def active_path(root: Path, mem: Memory) -> Path:
    """活动记忆的落盘绝对路径（namespaces/<ns>/<type>/<id>.md）。"""
    return ns_root(root) / mem.ns / mem.type / f"{mem.id}.md"


def archive_path(root: Path, mem: Memory) -> Path:
    """归档记忆的落盘绝对路径（archive/<ns>/<type>/<id>.md）。"""
    return archive_root(root) / mem.ns / mem.type / f"{mem.id}.md"


def active_rel(root: Path, mem: Memory) -> str:
    """活动记忆相对 root 的 POSIX 路径（索引/向量的 rel_path 口径）。"""
    # 统一 POSIX 分隔符：消费端（词面候选的 ns 前缀剪枝）按 "/" 匹配，
    # Windows 上 str(relative_to) 产出 "\" 会让检索候选被整体剪掉
    return active_path(root, mem).relative_to(root).as_posix()
