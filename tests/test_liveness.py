"""liveness 共享探测的单元测试：Index 与 VectorIndex 的带外增删协议单点。

测试编码"为什么重要"：活性协议曾因两个缓存各自复制而漂移出真 bug（词面侧
循环错位，非最后 ns 的带外新增永久隐形）。协议收拢一处后，判定语义只在这里
用 os.utime 确定性钉死——不依赖真实墙钟与文件系统遍历序，两个缓存的读路径
自愈共用这一份判定。
"""

from __future__ import annotations

import os
from pathlib import Path

from compound_memory.liveness import dirs_newer_than

STAMP = 1_000_000_000_000  # 任意基准 stamp；新旧关系由 os.utime 注入，不依赖真实时钟


def _touch(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _make_tree(ns_root: Path, layout: dict[str, list[str]]) -> None:
    """按 {ns: [type, ...]} 建 ns/type 两级目录树。"""
    for ns, types in layout.items():
        for t in types:
            (ns_root / ns / t).mkdir(parents=True, exist_ok=True)


def _all_older(ns_root: Path, layout: dict[str, list[str]]) -> None:
    """整棵树（含 ns_root）都拨到 stamp 之前。"""
    _touch(ns_root, STAMP - 10)
    for ns, types in layout.items():
        _touch(ns_root / ns, STAMP - 10)
        for t in types:
            _touch(ns_root / ns / t, STAMP - 10)


class TestDirsNewerThan:
    def test_type_dir_newer_in_first_namespace_returns_true(self, tmp_path: Path):
        """回归钉（漂移 bug 的最小复现）：只有排序靠前的 ns 的 type 目录晚于
        stamp ⇒ True。旧词面实现循环错位，只检查最后一个 ns 的 type 目录，
        这里会错判 False；探测按名字排序遍历，用例与遍历序解耦。"""
        ns_root = tmp_path / "namespaces"
        layout = {"ns_a": ["episode"], "ns_b": ["fact"]}
        _make_tree(ns_root, layout)
        _all_older(ns_root, layout)
        _touch(ns_root / "ns_a" / "episode", STAMP + 10)
        assert dirs_newer_than(ns_root, STAMP) is True

    def test_type_dir_newer_in_second_namespace_returns_true(self, tmp_path: Path):
        """对称形态：靠后的 ns 更新同样检出（防止修复退化为只查第一个）。"""
        ns_root = tmp_path / "namespaces"
        layout = {"ns_a": ["episode"], "ns_b": ["fact"]}
        _make_tree(ns_root, layout)
        _all_older(ns_root, layout)
        _touch(ns_root / "ns_b" / "fact", STAMP + 10)
        assert dirs_newer_than(ns_root, STAMP) is True

    def test_ns_dir_itself_newer_returns_true(self, tmp_path: Path):
        """新增整个 ns 目录（新宿主首次写入）只动 ns_root 与 ns 目录自身。"""
        ns_root = tmp_path / "namespaces"
        _make_tree(ns_root, {"ns_a": ["episode"]})
        _all_older(ns_root, {"ns_a": ["episode"]})
        _touch(ns_root / "ns_a", STAMP + 10)
        assert dirs_newer_than(ns_root, STAMP) is True

    def test_nothing_newer_returns_false(self, tmp_path: Path):
        ns_root = tmp_path / "namespaces"
        layout = {"ns_a": ["episode"], "ns_b": ["fact"]}
        _make_tree(ns_root, layout)
        _all_older(ns_root, layout)
        assert dirs_newer_than(ns_root, STAMP) is False

    def test_empty_ns_root_returns_false(self, tmp_path: Path):
        """回归钉：旧词面实现此处 type_dirs 未绑定直接 UnboundLocalError。"""
        ns_root = tmp_path / "namespaces"
        ns_root.mkdir()
        assert dirs_newer_than(ns_root, STAMP) is False

    def test_missing_ns_root_returns_false(self, tmp_path: Path):
        assert dirs_newer_than(tmp_path / "namespaces", STAMP) is False

    def test_ns_root_is_file_returns_false(self, tmp_path: Path):
        ns_root = tmp_path / "namespaces"
        ns_root.write_text("not a dir", encoding="utf-8")
        assert dirs_newer_than(ns_root, STAMP) is False
