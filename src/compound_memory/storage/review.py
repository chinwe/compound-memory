"""评审队列动词（动词件，#37）：review_queue / review_resolve / find_by_key（key 冲突域）。

平级件 review_queue.py（行格式生成/解析/清除的单一定义点）不动，本模块只收
facade 侧编排。锁语义逐位保持：review_resolve 的「清行 + 归档 + commit」一个
临界区、D2（#34）may_clear 行级属主可见性门禁随编排体原样迁移。
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Iterator, Protocol, overload

from ..model import Memory
from ..review_queue import ReviewQueue


class ReviewDeps(Protocol):
    """review 动词实际触碰的 store 面（窄 Deps，ADR 0003 裁决 4 / #28 R1）。

    _resolve_identity 镜像 facade 的 overload 对（mypy 结构匹配要求同构；
    调用点语义与 facade 内直调逐位一致：str 入 str 出、None 入可能 None 出）。
    """

    ns_root: Path
    _review_queue: ReviewQueue

    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]: ...
    def _check_ns_owner(self, ns: str, identity: str | None, role: str = "reader") -> None: ...
    def _write_lock(self) -> AbstractContextManager[None]: ...
    def find(self, mem_id: str) -> Memory | None: ...
    def _archive(self, mem: Memory) -> None: ...
    def _commit(self, message: str) -> None: ...

    @overload
    def _resolve_identity(self, value: str, role: str) -> str: ...
    @overload
    def _resolve_identity(self, value: None, role: str) -> str | None: ...
    def _resolve_identity(self, value: str | None, role: str) -> str | None: ...


def find_by_key(store: ReviewDeps, ns: str, mtype: str, key: str, exclude_content: str) -> Memory | None:
    base = store.ns_root / ns / mtype
    if not base.exists():
        return None
    for mem, _path in store._scan_parsed(base):
        if mem.key == key and mem.content.strip() != exclude_content.strip():
            return mem
    return None


def review_queue(store: ReviewDeps) -> list[str]:
    """冲突队列展示行——行格式的生成与解析都在 ReviewQueue。"""
    return store._review_queue.lines()


def review_resolve(
    store: ReviewDeps,
    ids: list[str] | None = None,
    all: bool = False,
    reader: str | None = None,
) -> dict[str, Any]:
    """登记冲突已解决：委托 ReviewQueue 清行，resolved>0 时自动 commit。

    裁决（新旧取舍）归调用方——按 ids 清行时，传入 id 即裁决的废置方，
    清行同时把该条归档（对侧保留活动区）；--all 只清行，不携带裁决信息，
    不自动归档。spec 非目标：不自动裁决冲突——归档跟随调用方指认的废置
    方，不做方向推断。归档必须跟随清行动作的教训（2026-10-05 运维）：
    清行不归档时废置旧版（uses=0）滞留活动区，且永不出现在 uses≥1 门槛
    的蒸馏候选里——同 key 多版本并存由此累积。

    D2（#34，2026-10-06 决议）：清行按属主可见性收口——agent-* 私有 ns
    的行仅属主可 resolve（可选 reader，与 get/search 同规）；--all 对不可
    见行静默保留（过滤），显式点名他人私有行 PermissionError 原子拒绝；
    _shared 行不受影响。review-queue 展示维持全量（张力见 spec：CLI 是
    本机信任边界，队列行含 content[:40] 片段，MCP 5 tool 不暴露队列）。
    """
    reader = store._resolve_identity(reader, "reader")

    def may_clear(ns: str) -> bool:
        # 行级清行许可（D2）：_shared 恒可清；agent-* 行按属主可见性
        # （fail-closed，身份未知视为不可清）。属主判定单点在
        # _check_ns_owner，这里只包装成谓词供 ReviewQueue 逐行调用。
        if not ns.startswith("agent-"):
            return True
        try:
            store._check_ns_owner(ns, reader)
        except PermissionError:
            return False
        return True

    with store._write_lock():  # 队列文件改写 + 归档 + 登记提交一个临界区
        out = store._review_queue.resolve(ids=ids, all=all, may_clear=may_clear)
        if all:
            out.pop("rows")  # --all 无废置信息，rows 不进返回（CLI 输出同理）
        archived: list[str] = []
        if not all and ids:
            wanted = set(ids)
            for row in out["rows"]:
                for mem_id in (row["old"], row["new"]):
                    if mem_id not in wanted or mem_id in archived:
                        continue
                    mem = store.find(mem_id)
                    if mem is None or mem.archived:
                        continue
                    store._archive(mem)
                    archived.append(mem_id)
        if out["resolved"]:
            message = f"review resolve {out['resolved']} entries"
            if archived:
                message += " (archived: " + ", ".join(archived) + ")"
            store._commit(message)
        out["archived"] = archived
    return out
