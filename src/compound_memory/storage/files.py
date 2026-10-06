"""文件 IO（机制件，#36 片 c）：MD + frontmatter 序列化与按 id 读。

单一定义点：记忆文件的正/反序列化（save/parse）、扫描路径的容错解析
（parse_for_scan/scan_parsed）、按 id 定位（find）；C 扩展 loader 的选择
（_SafeLoader）与默认删除 adapter（_unlink_file）随本模块。
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterator

import yaml

from ..index import atomic_write_text
from ..model import Memory
from . import paths
from .validation import _PATH_COMPONENT_RE

logger = logging.getLogger(__name__)

# frontmatter 解析 loader：C 扩展（libyaml）快 ~5x 且与 SafeLoader 语义逐位一致
# （perf-bench：scan_pairs 的 yaml parse 是对账/统计读路径的最大单项），
# 无 C 扩展的安装回退纯 Python loader——行为不变，只慢
try:
    from yaml import CSafeLoader as _SafeLoader
except ImportError:  # pragma: no cover - 取决于 PyYAML 是否带 C 扩展
    from yaml import SafeLoader as _SafeLoader  # type: ignore[assignment]


def _unlink_file(path: Path) -> None:
    """默认删除 adapter（测试侧经 conftest 注入沙箱安全版本）。"""
    if path.exists():
        path.unlink()


def save(mem: Memory, root: Path) -> None:
    """记忆序列化落盘（全部写动词共用的单文件写出点，#18/#21）。

    落点经 paths 推导：archived 记忆写归档区、活动记忆写活动区。"""
    path = paths.archive_path(root, mem) if mem.archived else paths.active_path(root, mem)
    path.parent.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {}
    for key, value in asdict(mem).items():
        if key == "content":
            continue
        if value is None or value == "" or value == []:
            continue
        if key in ("uses",) and value == 0:
            continue
        if key == "archived" and not value:
            continue
        meta[key] = value
    body = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n\n" + mem.content.strip() + "\n"
    # 原子写出收口到共享单点 atomic_write_text（#18/#21）：中断不留半写、
    # 临时名唯一、失败清理、权限对齐 open() 默认
    atomic_write_text(path, body)


def parse(path: Path) -> Memory:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"bad memory file (missing frontmatter): {path}")
    _, fm, body = text.split("---\n", 2)
    meta = yaml.load(fm, Loader=_SafeLoader) or {}
    # 合法 YAML 但非映射（手编标量/列表）：统一转解析失败（#20 容错面覆盖），
    # 否则下面 meta["content"] 抛 TypeError / meta.items() 抛 AttributeError 逃过捕获
    if not isinstance(meta, dict):
        raise ValueError(f"bad memory file (frontmatter not a mapping): {path}")
    meta["content"] = body.strip()
    defaults = {
        f.name: f.default
        for f in dataclasses.fields(Memory)
        if f.default is not dataclasses.MISSING and f.name != "content"
    }
    defaults.pop("content", None)
    # 缺必填字段（手编文件最常见坏法）转译为 ValueError：统一解析失败面，
    # 扫描容错与调用方无需各自特判 TypeError
    try:
        return Memory(**{**defaults, **{k: v for k, v in meta.items() if k in {f.name for f in dataclasses.fields(Memory)}}})
    except TypeError as exc:
        raise ValueError(f"bad memory file (missing required field): {path}: {exc}") from exc


def parse_for_scan(path: Path) -> Memory | None:
    """扫描路径的容错解析（#20）：坏文件跳过并告警，不炸整场扫描。

    只捕解析类异常（缺 frontmatter / 坏 YAML / 非映射 / 缺必填字段 /
    编码与读盘错误），其他异常照常传播。返回 None 表示跳过；
    文件本身不动，留给人工处置。全好文件零日志，告警即坏信号。
    """
    try:
        return parse(path)
    except (ValueError, OSError, yaml.YAMLError) as exc:
        logger.warning("skipping unparseable memory file %s: %s", path, exc)
        return None


def scan_parsed(base: Path) -> Iterator[tuple[Memory, Path]]:
    """按目录扫描 *.md 并容错解析（#20 扫描消费方共用单点）。

    逐条告警之外，结束时对跳过数量做一次汇总告警（#20 验收：
    数量 + 逐条路径 + 原因，两层都有）。"""
    skipped = 0
    for path in sorted(base.rglob("*.md")):
        mem = parse_for_scan(path)
        if mem is None:
            skipped += 1
            continue
        yield mem, path
    if skipped:
        logger.warning("scan skipped %d unparseable memory file(s)", skipped)


def find(mem_id: str, root: Path) -> Memory | None:
    # mem_id 拼 rglob 模式：非法字符（glob 元字符/路径分隔）不得进入——
    # '*' 曾命中库内任意第一条且绕过属主检查直泄私有正文（审计 P2-3）。
    # 非法 id 语义等价于「不可能存在」⇒ 返回 None：全部调用方对 None
    # 已有容错分支，抛错反而会炸掉邻居召回的「宁缺勿炸」降级。
    if not _PATH_COMPONENT_RE.match(mem_id):
        return None
    for base in (paths.ns_root(root), paths.archive_root(root)):
        for path in base.rglob(f"{mem_id}.md"):
            return parse(path)
    return None
