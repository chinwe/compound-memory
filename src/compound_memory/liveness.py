"""缓存活性共享探测与 scan 共享窗口：Index 与 VectorIndex「带外增删」协议
与「对账 scan」的单一定义点。

历史教训：两级目录 mtime 探测曾在两个缓存里各复制一份，词面侧副本循环
错位（type_dirs 在 ns 循环内赋值、循环外消费），非最后 ns 的带外新增永久
隐形，空 ns 目录还会 UnboundLocalError——活性协议因此收拢到这里。

不做「缓存未变即未 stale」的短路：手编已有文件只动目录 mtime 不动缓存，
短路会漏检；显式 rebuild 由调用方负责。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from .model import Memory


def dirs_newer_than(ns_root: Path, stamp: int) -> bool:
    """ns/type 两级目录任一 mtime 晚于 stamp 即 True（带外新增/删除会更新父目录 mtime）。

    遍历按名字排序（确定性行为，测试可控）；OSError 逐目录降级 continue——
    单个目录 stat 失败不放大成探测整体失败（检索降级不报错）；ns_root 缺失
    或不可读 ⇒ False。
    """
    try:
        ns_dirs = sorted(d for d in ns_root.iterdir() if d.is_dir())
    except OSError:
        return False
    for ns_dir in ns_dirs:
        try:
            if ns_dir.stat().st_mtime_ns > stamp:
                return True
            type_dirs = [d for d in ns_dir.iterdir() if d.is_dir()]
        except OSError:
            continue
        for t_dir in type_dirs:
            try:
                if t_dir.stat().st_mtime_ns > stamp:
                    return True
            except OSError:
                continue
    return False


class ScanWindow:
    """一次公开读动词内的 scan_pairs 共享窗口（#41 共享 scan）。

    读路径的两份缓存（Index / VectorIndex）各自对账时都要 scan_pairs
    （全库 rglob + parse），同一动词内的第二遍是纯重复——万条库一遍 scan
    就是秒级。窗口由公开检索动词开启（open），窗口内的 scan 复用同一份
    结果；窗口外（显式 rebuild、写路径保活）恒走 fresh scan——手编内容后
    的 rebuild 补救不能吃缓存 scan（手编不改目录 mtime，缓存 scan 探测不到）。

    返回同一 list 对象：两份缓存对账只迭代不修改（谁要修改谁先 copy）。
    """

    def __init__(self) -> None:
        self._pairs: list[tuple[Memory, str]] | None = None

    def open(self) -> None:
        """开新窗口：上一窗口的 scan 结果不复用（跨读动词的磁盘变更不可见）。"""
        self._pairs = None

    def pairs(self, scan: Callable[[], list[tuple[Memory, str]]]) -> list[tuple[Memory, str]]:
        """窗口内取 scan 结果：首次调用执行 scan 并暂存，后续直接复用。"""
        if self._pairs is None:
            self._pairs = scan()
        return self._pairs
