"""契约套件共享助手：git 提交锚点（#31 消息模板）与身份常量。

契约 = characterization：全部断言钉当前实现行为，必须对现状跑绿
（#25 决议：tests/contracts/ 是唯一带强制力的契约载体）。
side effects 要素锚定 git 提交——模板漂移（分隔符/字段顺序/措辞）在此必红。
消息模板表与 docs/contracts/README.md 同源，改动即「契约变更」。
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess

from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE

# 权限矩阵测试共用的身份常量（与 test_ns_isolation.py 同约定）
OWNER = "agent-zcode"
OWNER_BARE = "zcode"
FOREIGN = "agent-workbuddy"
PRIVATE_NS = "agent-zcode"

# #31 决议钉定的写路径提交消息模板（七写动词 + batch 两形态 + 三类特殊提交中的两个）。
# 正则整体锚定（^...$），分组名供断言取字段。
# 契约变更（#53，ADR-0007/0008 背书）：feedback 模板扩 outcome= 段（证据事件进
# 提交消息，git 历史即证据全史）；review_resolve 模板扩可选 (upheld: ...) 段
# （contradiction 裁决「维持」的折算留痕）。
COMMIT_TEMPLATES: dict[str, str] = {
    "write": r"^write (?P<id>\S+) \((?P<type>episode|fact|insight|skill)/(?P<ns>\S+)\) by (?P<source>\S+)$",
    "feedback": r"^feedback (?P<id>\S+) by (?P<agent>\S+): outcome=(?P<outcome>\S+) uses=(?P<uses>\d+) conf=(?P<conf>\d+(?:\.\d+)?)$",
    "link": r"^link (?P<a>\S+) <-> (?P<b>\S+)$",
    "decay": r"^decay: archive (?P<ids>\S+(?:, \S+)*)$",
    "revive": r"^revive (?P<id>\S+)$",
    "distill_apply": r"^distill apply (?P<id>\S+) <- (?P<sources>\S+(?:, \S+)*)$",
    "review_resolve": r"^review resolve (?P<n>\d+) entries(?: \(archived: (?P<ids>\S+(?:, \S+)*)\))?(?: \(upheld: (?P<upheld>\S+(?:, \S+)*)\))?$",
    "batch": r"^batch write (?P<n>\d+) entries(?: \(partial\))?$",
    "init": r"^init compound-memory store$",
    "orphan": r"^orphan changes recovered$",
}


def days_ago(n: int) -> str:
    """相对测试固定"今天"（CLOCK_DATE）的 ISO 日期——与 store fixture 的 clock 同源。"""
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


def messages(store: MemoryStore, limit: int = 50) -> list[str]:
    """git log 的纯消息列表（剥 --oneline 的 hash 前缀），最新在前。"""
    return [line.split(" ", 1)[1] for line in store.git_log(limit) if " " in line]


def last_message(store: MemoryStore) -> str:
    """最新一条提交消息（side effects 锚点断言的主入口）。"""
    log = messages(store, 1)
    assert log, "git log is empty: expected at least one commit"
    return log[0]


def commit_count(store: MemoryStore) -> int:
    """HEAD 提交总数——「恰好一次 / 零次 commit」的精确断言用。"""
    proc = subprocess.run(
        ["git", "-C", str(store.root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def matches(template: str, message: str) -> re.Match[str]:
    """断言提交消息命中 #31 模板，返回分组供字段级断言。"""
    pattern = COMMIT_TEMPLATES[template]
    m = re.match(pattern, message)
    assert m is not None, (
        f"commit message {message!r} does not match contract template {template}: {pattern!r} "
        "(commit message templates are pinned by issue #31; changing one is a contract change)"
    )
    return m
