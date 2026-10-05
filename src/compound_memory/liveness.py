"""缓存活性共享探测：Index 与 VectorIndex「带外增删」协议的单一定义点。

历史教训：两级目录 mtime 探测曾在两个缓存里各复制一份，词面侧副本循环
错位（type_dirs 在 ns 循环内赋值、循环外消费），非最后 ns 的带外新增永久
隐形，空 ns 目录还会 UnboundLocalError——活性协议因此收拢到这里。

不做「缓存未变即未 stale」的短路：手编已有文件只动目录 mtime 不动缓存，
短路会漏检；显式 rebuild 由调用方负责。
"""

from __future__ import annotations

from pathlib import Path


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
