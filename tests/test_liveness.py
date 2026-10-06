"""liveness 共享探测的单元测试：Index 与 VectorIndex 的带外增删协议单点。

测试编码"为什么重要"：活性协议曾因两个缓存各自复制而漂移出真 bug（词面侧
循环错位，非最后 ns 的带外新增永久隐形）。协议收拢一处后，判定语义只在这里
用 os.utime 确定性钉死——不依赖真实墙钟与文件系统遍历序，两个缓存的读路径
自愈共用这一份判定。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from compound_memory.liveness import ScanWindow, dirs_newer_than
from compound_memory.model import Memory

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


ScanFn = Callable[[], list[tuple[Memory, str]]]


def make_scan(calls: list[int]) -> ScanFn:
    """计数 scan：每次调用记一笔，返回固定单条 pairs。"""

    def scan() -> list[tuple[Memory, str]]:
        calls.append(1)
        return [(Memory(id="m", ns="_shared", type="fact", source="t", created="2026-01-01", content="x"), "r")]

    return scan


class TestScanWindow:
    """读动词内的 scan 共享窗口（#41）：词法/向量对账共享一遍 scan 的协议单点。

    钉三件事：窗口内第二次取不重扫（共享的本体）；open 开新窗口后不复用
    （跨读动词的磁盘变更不可见）；窗口外首取恒 fresh。计数器即「scan 发生
    过几次」的可观测面——对账共享的正确性基准是调用次数，不是结果内容。
    """

    def _window_with_counter(self) -> tuple[ScanWindow, list[int], ScanFn]:
        calls: list[int] = []
        win = ScanWindow()
        scan: ScanFn = make_scan(calls)
        return win, calls, scan

    def test_second_call_within_window_reuses_scan(self):
        win, calls, scan = self._window_with_counter()
        first = win.pairs(scan)
        assert win.pairs(scan) is first  # 同一对象：两份缓存对账共用一遍 scan
        assert sum(calls) == 1

    def test_open_resets_window(self):
        win, calls, scan = self._window_with_counter()
        win.pairs(scan)
        win.open()
        win.pairs(scan)
        assert sum(calls) == 2  # 新窗口不复用上一窗口的 scan

    def test_returns_same_list_object_not_a_copy(self):
        """消费方（两份缓存对账）只迭代不修改的前提：窗口交出的是同一份引用。"""
        win, _, scan = self._window_with_counter()
        assert win.pairs(scan) is win.pairs(scan)
