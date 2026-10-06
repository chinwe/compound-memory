"""Review queue（冲突队列）：review-queue.md 的生成、解析与清除——行格式单一定义点。

行型两种（append 生成，机器写入）：①同 key fact/insight 内容冲突行，由
MemoryStore._write_new 登记；②contradiction 争议行（ADR-0007），由
feedback outcome=contradiction 登记，待裁决期间该记忆数值冻结（冻结判定
经 pending_contradictions 派生，行清掉即解冻）。裁决（取舍/维持）归调用方，
这里只登记、展示与清除，不做判断、不碰 git。

行结构：冲突行 old/new 记忆 id 各在 "(` 与 " (" 边界；非贪婪到首个 " vs "
——content 清洗控制字符后截断 40 字符，正则回溯保证内容含 " vs " 时仍取对
id。contradiction 行 `- <date> contradiction <mem_id>: by <agent> (<正文前 40
字>)`——feedback 签名固定 (mem_id, agent, outcome) 无备注通道（ADR-0008），
括号段承载记忆正文片段供裁决上下文（与冲突行的正文片段同隐私口径）。解析
失败的行 fail-safe 保留：宁可不登记，不误删记录。
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Callable

from .index import atomic_write_text
from .model import Memory

_REVIEW_ROW_RE = re.compile(r"^- \S+ conflict `(?P<ns>[^/`]+)/[^`]+`: (?P<old>\S+) \(.*?\) vs (?P<new>\S+) \(")

# contradiction 独立行型（ADR-0007）：只锚定到 "by <agent> ("，正文片段自由文本
# 不参与解析（roundtrip 只需 mem_id 与 agent）。
_CONTRADICTION_ROW_RE = re.compile(r"^- \S+ contradiction (?P<mem>\S+): by (?P<agent>\S+) \(")

# 行格式是机器可解析契约：自由文本（ns/key/source/content）里的控制字符
# （换行、制表等）曾可把一行拆成两行，损坏行被 fail-safe 保留后永久占队列
# （2026-10-05 审计 P2-4）——写入前在 append 单点中性化为空格。
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

# resolve 内部行结构（kind 区分行型；contradiction 行不携带 ns，由调用方按
# 记忆定位）。返回给调用方的 rows 只含 {"old","new"}——形状是既有契约。
_ROW_KIND_CONFLICT = "conflict"
_ROW_KIND_CONTRADICTION = "contradiction"


def _sanitize(text: str) -> str:
    return _CTRL_CHARS_RE.sub(" ", text)


def _parse_row(line: str) -> dict[str, str] | None:
    """行 → 内部结构（kind/ns/old/new）；两种已知行型之外返回 None（fail-safe 保留）。"""
    m = _REVIEW_ROW_RE.match(line)
    if m is not None:
        return {"kind": _ROW_KIND_CONFLICT, "ns": m.group("ns"), "old": m.group("old"), "new": m.group("new")}
    m = _CONTRADICTION_ROW_RE.match(line)
    if m is not None:
        # contradiction 行无 ns 段（ADR-0007 钉死行格式）：ns 由调用方经记忆定位
        return {"kind": _ROW_KIND_CONTRADICTION, "ns": "", "old": m.group("mem"), "new": m.group("mem")}
    return None


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

    def append_contradiction(self, mem: Memory, agent: str) -> None:
        """contradiction 争议行登记（ADR-0007）：feedback outcome=contradiction 单点调用。"""
        line = (
            f"- {self._clock().isoformat()} contradiction {_sanitize(mem.id)}: "
            f"by {_sanitize(agent)} ({_sanitize(mem.content)[:40]})\n"
        )
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    def pending_contradictions(self) -> set[str]:
        """未裁决 contradiction 行的记忆 id 集（冻结判定单点：feedback 据此数值冻结）。"""
        if not self.path.exists():
            return set()
        pending: set[str] = set()
        for line in self.path.read_text(encoding="utf-8").splitlines():
            m = _CONTRADICTION_ROW_RE.match(line)
            if m is not None:
                pending.add(m.group("mem"))
        return pending

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
        may_clear: Callable[[dict[str, str]], bool] | None = None,
        uphold: bool = False,
    ) -> dict[str, Any]:
        """清除命中行，返回 {"resolved", "remaining", "rows"}；git commit 归调用方（MemoryStore）。

        - all=True：清空整个队列（幂等，空队列返回 resolved=0）。
        - 按 id：行内 old/new 任一命中即整行清除；任一 id 未命中任何行 ⇒
          ValueError 原子拒绝（队列原样保留），避免半清状态让调用方误判。
        - uphold=True（ADR-0007 裁决「维持」）：命中行必须是 contradiction 行型
          （冲突行没有「维持」语义 ⇒ ValueError 原子拒绝）；行清除且**不归档**，
          折算（failure −0.2）归调用方编排。
        - rows 是被清行的 (old, new) 明细：调用方凭「传入 id = 裁决废置方」
          归档淘汰侧；这里只解析行结构，不归档、不做方向判断。
        - may_clear(row)（D2/#34 属主可见性过滤，None = 不过滤，行为同旧版）：
          冲突行自带 ns（行格式 `` `ns/type/key` ``）；contradiction 行不携带 ns，
          row["ns"] 为空串、由调用方按记忆定位——行级许可由调用方裁决——
          - 按 id 命中的行若不可清 ⇒ PermissionError 原子拒绝（显式点名
            他人私有行是越权，响亮拒绝而非静默半清）；
          - --all 只清可清行，其余（含解析不出归属的行，fail-safe 宁留勿删）
            原样保留，remaining 如实计数。
        """
        if all and ids:
            raise ValueError("pass either ids or --all, not both")
        if uphold and all:
            raise ValueError("uphold resolves specific contradiction rows; pass ids, not --all")
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
                    row = _parse_row(line)
                    if row is not None and may_clear(row):
                        removed += 1
                        continue
                    keep.append(line)
            # all 模式不产出 rows：没有裁决信息，rows 无消费方（归档只跟 ids 走）
        else:
            wanted = set(ids or [])
            covered: set[str] = set()
            for line in lines:
                row = _parse_row(line)
                hit = row is not None and bool(wanted & {row["old"], row["new"]})
                if hit and row is not None:
                    if may_clear is not None and not may_clear(row):
                        raise PermissionError(
                            f"review-queue row in private namespace {row['ns']!r}: "
                            "only its owner host can resolve it (pass the owner as reader)"
                        )
                    if uphold and row["kind"] != _ROW_KIND_CONTRADICTION:
                        # 原子拒绝：raise 发生在任何写盘之前，队列原样保留
                        raise ValueError(
                            "uphold applies only to contradiction rows; "
                            f"{row['old']!r} sits in a {row['kind']} row"
                        )
                    rows.append({"old": row["old"], "new": row["new"]})
                if row is not None:
                    covered |= {row["old"], row["new"]}
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
