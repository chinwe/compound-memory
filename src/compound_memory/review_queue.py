"""Review queue（冲突队列）：review-queue.md 的生成、解析与清除——行格式单一定义点。

每行一条冲突记录，由 MemoryStore._write_new 在同 key fact/insight/decision 内容冲突时 append；
裁决（新旧取舍）归调用方，这里只登记、展示与清除，不做判断、不碰 git。

行结构（append 生成，机器写入）：old/new 记忆 id 各在 "(` 与 " (" 边界；
非贪婪到首个 " vs "——content 清洗控制字符后截断 40 字符，正则回溯保证内容
含 " vs " 时仍取对 id。解析失败的行 fail-safe 保留：宁可不登记，不误删记录。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Callable

from .index import atomic_write_text
from .model import Memory

_REVIEW_ROW_RE = re.compile(r"^- \S+ conflict `(?P<ns>[^/`]+)/[^`]+`: (?P<old>\S+) \(.*?\) vs (?P<new>\S+) \(")

# 行格式是机器可解析契约：自由文本（ns/key/source/content）里的控制字符
# （换行、制表等）曾可把一行拆成两行，损坏行被 fail-safe 保留后永久占队列
# （2026-10-05 审计 P2-4）——写入前在 append 单点中性化为空格。
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize(text: str) -> str:
    return _CTRL_CHARS_RE.sub(" ", text)


class ReviewQueue:
    """冲突队列 artifact 的所有者：append 写入、lines 展示、resolve 清除。"""

    def __init__(self, path: Path, clock: Callable[[], dt.date]) -> None:
        self.path = path
        self._clock = clock

    def append(self, old: Memory, new: Memory) -> None:
        line = (
            f"- {self._clock().isoformat()} conflict `{_sanitize(new.ns)}/{new.type}/{_sanitize(new.key or '')}`: "
            f"{old.id} ({_sanitize(old.source)}: {_sanitize(old.content)[:40]}) vs "
            f"{new.id} ({_sanitize(new.source)}: {_sanitize(new.content)[:40]})\n"
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

    def resolve(
        self,
        ids: list[str] | None = None,
        all: bool = False,
        may_clear: Callable[[str], bool] | None = None,
    ) -> dict[str, Any]:
        """清除命中行，返回 {"resolved", "remaining", "rows"}；git commit 归调用方（MemoryStore）。

        - all=True：清空整个队列（幂等，空队列返回 resolved=0）。
        - 按 id：行内 old/new 任一命中即整行清除；任一 id 未命中任何行 ⇒
          ValueError 原子拒绝（队列原样保留），避免半清状态让调用方误判。
        - rows 是被清行的 (old, new) 明细：调用方凭「传入 id = 裁决废置方」
          归档淘汰侧；这里只解析行结构，不归档、不做方向判断。
        - may_clear(ns)（D2/#34 属主可见性过滤，None = 不过滤，行为同旧版）：
          冲突行自带 ns（行格式 `` `ns/type/key` ``），行级许可由调用方裁决——
          - 按 id 命中的行若不可清 ⇒ PermissionError 原子拒绝（显式点名
            他人私有行是越权，响亮拒绝而非静默半清）；
          - --all 只清可清行，其余（含解析不出 ns 的行，fail-safe 宁留勿删）
            原样保留，remaining 如实计数。
        """
        if all and ids:
            raise ValueError("pass either ids or --all, not both")
        if not all and not ids:
            raise ValueError("review-resolve needs memory ids or --all")
        if self.path.exists():
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        else:
            lines = []
        rows: list[dict[str, str]] = []
        keep: list[str] = []
        if all:
            if may_clear is None:
                removed = sum(1 for line in lines if line.startswith("- "))
            else:
                removed = 0
                for line in lines:
                    m = _REVIEW_ROW_RE.match(line)
                    if m is not None and may_clear(m.group("ns")):
                        removed += 1
                        continue
                    keep.append(line)
            # all 模式不产出 rows：没有裁决信息，rows 无消费方（归档只跟 ids 走）
        else:
            wanted = set(ids or [])
            covered: set[str] = set()
            for line in lines:
                m = _REVIEW_ROW_RE.match(line)
                hit = m is not None and bool(wanted & {m.group("old"), m.group("new")})
                if hit and m is not None:
                    if may_clear is not None and not may_clear(m.group("ns")):
                        raise PermissionError(
                            f"review-queue row in private namespace {m.group('ns')!r}: "
                            "only its owner host can resolve it (pass the owner as reader)"
                        )
                    rows.append({"old": m.group("old"), "new": m.group("new")})
                if m is not None:
                    covered |= {m.group("old"), m.group("new")}
                if not hit:
                    keep.append(line)
            missing = wanted - covered
            if missing:
                raise ValueError(f"ids not found in review queue: {', '.join(sorted(missing))}")
            removed = len(rows)
        if removed:
            # 原子写共享单点：清行改写中断时旧队列原封保留（spec：文件写出一律 atomic_write_text）
            atomic_write_text(self.path, "".join(keep))
        return {
            "resolved": removed,
            "remaining": sum(1 for line in keep if line.startswith("- ")),
            "rows": rows,
        }
