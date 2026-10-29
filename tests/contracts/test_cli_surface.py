"""CLI 层契约（#25 决议第三优先级）：只钉命令面存在性，行为不重测。

CLI 是薄 adapter：26 个子命令名 + 关键 flags（含 D1/D2 的 link --agent 与
review-resolve --reader、ADR-0009 的 forget --agent/--reason、#44 的
explain --reader 与 search --explain、#69 的 dream --ns/--reader、#72 的
dream-apply --proposals/--source/--dry-run、doctor 健康检查、maintain 的
--with-decay、ADR-0014 的 skill-export / skill-export-apply）不消失即契约；
结构断言走 argparse 公开解析结果（flag 被消费 = 存在，落进 unknown = 不存在）。
"""

from __future__ import annotations

import argparse

import pytest

from compound_memory.cli import build_parser

# 24 个子命令（#25 决议钉定的命令面；#48/ADR-0009 契约变更：16→17 加 forget；
# #44/spec-52 Explain 载体裁决的契约变更：17→18 加 explain——按 id 证据视图；
# #69 契约变更：18→19 加 dream——CLI-only 只读编排，MCP 仍为 5；
# #72 契约变更：19→20 加 dream-apply——显式半自动落库，dream 仍只读，MCP 仍为 5；
# 契约变更：20→21 加 doctor——CLI 健康检查，MCP 仍为 5。
# #97 契约变更：dream 增加 --propose（默认关，写候选包，不 apply）。
# #100 契约变更：21→23 加 health-snapshot / health-trend——健康时序 CLI-only
# 运维动词（stats 单一指标源），MCP 仍为 5。
# 契约变更：23→24 加 maintain——CLI 日常维护编排（dream → 可选 decay →
# health-snapshot）。不带 --propose，不调用 dream-apply，不新增 MCP tool。
# 不加入 git-revert / dream --apply / dream --auto。
# 契约变更：24→26 加 skill-export / skill-export-apply（ADR-0014）。
# CLI-only，不进 maintain / dream / 定时脚本，不新增 MCP tool。
EXPECTED_COMMANDS = {
    "init", "write", "search", "get", "explain", "link", "feedback", "decay", "revive", "forget",
    "distill-plan", "distill-apply", "dream", "dream-apply", "stats", "doctor", "rebuild-index",
    "review-queue", "review-resolve", "git-log", "extract", "health-snapshot", "health-trend",
    "maintain", "skill-export", "skill-export-apply",
}


def _subcommands(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return dict(action.choices)


def _flag_consumed(parser: argparse.ArgumentParser, argv: list[str]) -> bool:
    """flag 存在 ⇔ argparse 把它从 argv 消费掉（unknown 为空）。"""
    _, unknown = parser.parse_known_args(argv)
    return not unknown


class TestCommandSurface:
    def test_exactly_twenty_six_subcommands(self):
        assert set(_subcommands(build_parser())) == EXPECTED_COMMANDS


class TestKeyFlags:
    """关键 flags 不消失：身份类（D1/D2 新增）、范围类、运维类各取所钉。"""

    @pytest.mark.parametrize(
        ("command", "argv"),
        [
            # 身份透传：link --agent（D1）/ review-resolve --reader（D2）/
            # forget --agent/--reason（ADR-0009，私有 ns 属主门禁与动机短语）/
            # get / search / revive / distill-plan 的 --reader
            ("link", ["link", "a", "b", "--agent", "zcode"]),
            ("review-resolve", ["review-resolve", "--reader", "zcode"]),
            ("forget-agent", ["forget", "id", "--agent", "zcode"]),
            ("forget-reason", ["forget", "id", "--agent", "zcode", "--reason", "superseded"]),
            ("get", ["get", "id", "--reader", "zcode"]),
            ("search", ["search", "q", "--ns", "agent-zcode", "--reader", "zcode"]),
            # explain 按 id 证据视图（#44 契约变更）：--reader 同 get 的私有 ns 门
            ("explain-reader", ["explain", "id", "--reader", "zcode"]),
            ("search-explain", ["search", "q", "--explain"]),
            ("revive", ["revive", "id", "--reader", "zcode"]),
            ("distill-plan", ["distill-plan", "--ns", "agent-zcode", "--reader", "zcode"]),
            # dream 只读编排（#69 契约变更）：--ns / --reader 与 distill-plan 同门
            ("dream-reader", ["dream", "--ns", "agent-zcode", "--reader", "zcode"]),
            ("dream-window", ["dream", "--window", "7", "--min-uses", "0", "--min-confidence", "0.2"]),
            # #97：显式候选包。不是 --auto，也不是 --apply
            ("dream-propose", ["dream", "--propose"]),
            # dream-apply 显式落库（#72 契约变更）：产物包 + 写者身份 + dry-run；无 --apply / --include-forget
            ("dream-apply-dry-run", ["dream-apply", "--proposals", "p.json", "--source", "agent-a", "--dry-run"]),
            (
                "dream-apply-reader",
                ["dream-apply", "--proposals", "p.json", "--source", "zcode", "--ns", "agent-zcode", "--reader", "zcode"],
            ),
            # search 的范围/形状 flags
            ("search-top-k", ["search", "q", "--top-k", "10"]),
            ("search-no-neighbors", ["search", "q", "--no-neighbors"]),
            # write 的落库 flags
            ("write-ns", ["write", "c", "fact", "a", "--ns", "_shared"]),
            ("write-key", ["write", "c", "fact", "a", "--key", "k"]),
            ("write-validity", ["write", "c", "fact", "a", "--valid-from", "2026-01-01", "--valid-until", "2026-12-31"]),
            # project 作用域（ADR 0010）：write/search/get 三动词的 --project
            ("write-project", ["write", "c", "fact", "a", "--project", "agenthub"]),
            # lesson_role（#78 契约变更）：write 的可选教训角色，不是新子命令
            ("write-lesson-role", ["write", "c", "decision", "a", "--lesson-role", "avoid"]),
            ("search-project", ["search", "q", "--project", "agenthub"]),
            ("get-project", ["get", "id", "--project", "agenthub"]),
            # distill-apply 的溯源 flags
            ("distill-apply-sources", ["distill-apply", "c", "insight", "a", "--sources", "id1,id2"]),
            ("distill-apply-confidence", ["distill-apply", "c", "insight", "a", "--sources", "id1", "--confidence", "0.7"]),
            # feedback 的 outcome 维度（ADR-0007）/ review-resolve 的维持裁决（参数扩展非新命令）
            ("feedback-outcome", ["feedback", "id", "agent", "--outcome", "failure"]),
            ("review-resolve-uphold", ["review-resolve", "--uphold"]),
            # 运维类
            ("decay-now", ["decay", "--now", "2026-10-01"]),
            ("maintain-with-decay", ["maintain", "--with-decay"]),
            ("maintain-now", ["maintain", "--now", "2026-10-01"]),
            ("maintain-reader", ["maintain", "--ns", "agent-zcode", "--reader", "zcode"]),
            # ADR-0014：导出是独立 CLI。project 精确匹配，apply 必须点名 target 与 items。
            (
                "skill-export-reader",
                ["skill-export", "--ns", "agent-zcode", "--reader", "zcode", "--project", "agenthub",
                 "--min-uses", "0", "--min-confidence", "0.5"],
            ),
            (
                "skill-export-apply-dry-run",
                ["skill-export-apply", "--items", "items.json", "--target", "cursor-project",
                 "--workspace", "/tmp/ws", "--dry-run"],
            ),
            (
                "skill-export-apply-dir",
                ["skill-export-apply", "--items", "items.json", "--target", "dir", "--out", "/tmp/out",
                 "--replace"],
            ),
            (
                "skill-export-apply-user",
                ["skill-export-apply", "--items", "items.json", "--target", "cursor-user"],
            ),
            ("review-resolve-all", ["review-resolve", "--all"]),
            ("git-log-limit", ["git-log", "--limit", "10"]),
        ],
    )
    def test_flag_exists(self, command: str, argv: list[str]):
        assert _flag_consumed(build_parser(), argv), f"flag disappeared from CLI surface: {argv}"

    def test_unknown_flag_is_not_consumed(self):
        """对照组：不存在的 flag 落进 unknown——存在性断言的判别力证明。"""
        assert not _flag_consumed(build_parser(), ["link", "a", "b", "--bogus", "x"])

    def test_dream_stays_free_of_apply_switches(self):
        """#72：dream 不挂 --apply；#97：不挂 --auto。dream-apply 不挂 --include-forget；无 git-revert。"""
        assert not _flag_consumed(build_parser(), ["dream", "--ns", "_shared", "--apply"])
        assert not _flag_consumed(build_parser(), ["dream", "--auto"])
        assert not _flag_consumed(
            build_parser(),
            ["dream-apply", "--proposals", "p.json", "--source", "agent-a", "--include-forget"],
        )
        assert "git-revert" not in _subcommands(build_parser())
        # maintain 编排只读 dream，不挂提议/落库开关
        assert not _flag_consumed(build_parser(), ["maintain", "--propose"])
        assert not _flag_consumed(build_parser(), ["maintain", "--apply"])
        assert not _flag_consumed(build_parser(), ["maintain", "--export"])
        assert not _flag_consumed(build_parser(), ["dream", "--export"])
        assert not _flag_consumed(build_parser(), ["skill-export", "--propose"])
