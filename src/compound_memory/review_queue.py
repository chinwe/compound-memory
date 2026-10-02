"""Review queue（冲突队列）：review-queue.md 的生成、解析与清除——行格式单一定义点。

每行一条冲突记录，由 MemoryStore._write_new 在同 key fact/insight 内容冲突时 append；
裁决（新旧取舍）归调用方，这里只登记、展示与清除，不做判断、不碰 git。

行结构（append 生成，机器写入）：old/new 记忆 id 各在 "(` 与 " (" 边界；
非贪婪到首个 " vs "——content 截断 40 字符，正则回溯保证内容含 " vs " 时仍取对 id。
解析失败的行 fail-safe 保留：宁可不登记，不误删记录。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Callable

from .model import Memory

_REVIEW_ROW_RE = re.compile(r"^- \S+ conflict `[^`]+`: (?P<old>\S+) \(.*?\) vs (?P<new>\S+) \(")


class ReviewQueue:
    """冲突队列 artifact 的所有者：append 写入、lines 展示、resolve 清除。"""

    def __init__(self, path: Path, clock: Callable[[], dt.date]) -> None:
        self.path = path
        self._clock = clock

    def append(self, old: Memory, new: Memory) -> None:
        line = (
            f"- {self._clock().isoformat()} conflict `{new.ns}/{new.type}/{new.key}`: "
            f"{old.id} ({old.source}: {old.content[:40]}) vs {new.id} ({new.source}: {new.content[:40]})\n"
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def lines(self) -> list[str]:
        """展示行（去 "- " 前缀，即 CLI review-queue 的输出）。"""
        if not self.path.exists():
            return []
        return [
            line[2:].rstrip("\n")
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.startswith("- ")
        ]

    def resolve(self, ids: list[str] | None = None, all: bool = False) -> dict[str, int]:
        """清除命中行，返回 {"resolved", "remaining"}；git commit 归调用方（MemoryStore）。

        - all=True：清空整个队列（幂等，空队列返回 resolved=0）。
        - 按 id：行内 old/new 任一命中即整行清除；任一 id 未命中任何行 ⇒
          ValueError 原子拒绝（队列原样保留），避免半清状态让调用方误判。
        """
        if all and ids:
            raise ValueError("pass either ids or --all, not both")
        if not all and not ids:
            raise ValueError("review-resolve needs memory ids or --all")
        if self.path.exists():
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        else:
            lines = []
        if all:
            keep = []
            removed = sum(1 for line in lines if line.startswith("- "))
        else:
            wanted = set(ids or [])
            covered: set[str] = set()
            matched: list[bool] = []
            for line in lines:
                m = _REVIEW_ROW_RE.match(line)
                matched.append(m is not None and bool(wanted & {m.group("old"), m.group("new")}))
                if m is not None:
                    covered |= {m.group("old"), m.group("new")}
            missing = wanted - covered
            if missing:
                raise ValueError(f"ids not found in review queue: {', '.join(sorted(missing))}")
            keep = [line for line, hit in zip(lines, matched) if not hit]
            removed = sum(matched)
        if removed:
            self.path.write_text("".join(keep), encoding="utf-8")
        return {"resolved": removed, "remaining": sum(1 for line in keep if line.startswith("- "))}
