"""git-log CLI 过滤 flags（#42）：--grep / --exclude 的呈现层过滤语义。

#31 决议的消费端降噪手段：MCP git_log 不动（返回形状红线），过滤只在
CLI 呈现层做。匹配原语统一 Python re.search（substring/regex，大小写
敏感）——对齐 git log --grep 的消息匹配本质；git 原生 --exclude 是
ref/pathspec 排除、对 commit message 不生效，故不透传 git log 参数
（避免 BRE 与 Python re 两套正则方言的语义漂移）。语义：--grep 正选
（多值 OR，任一命中保留）、--exclude 反选（任一命中剔除）、先取
--limit 窗口再过滤、匹配对象是剥掉 oneline hash 前缀后的消息段。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from compound_memory import cli as cli_mod
from compound_memory.cli import build_parser
from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE, sandbox_safe_remove


def _seeded_root(tmp_path: Path) -> Path:
    """4 条提交的审计史（init / write fact / write insight / feedback）——过滤断言素材。"""
    root = tmp_path / "memroot"
    store = MemoryStore(root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
    fact = store.write("gitlog filter fact one", type="fact", source="agent-a")
    store.write("gitlog filter insight two", type="insight", source="agent-a")
    store.feedback(fact["id"], "agent-b")
    assert len(store.git_log(10)) == 4  # 素材就位
    return root


def _run_git_log(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], root: Path, *extra: str
) -> tuple[int, list[str]]:
    """端到端跑 main()：拦截 auto_encoder（不依赖本机 vec 模型），返回 (rc, 输出行)。"""
    monkeypatch.setattr(cli_mod, "auto_encoder", lambda: None)
    rc = cli_mod.main(["--root", str(root), "git-log", "--limit", "10", *extra])
    out = capsys.readouterr().out
    lines: Any = json.loads(out) if out.strip() else []
    return rc, list(lines)


def _messages(lines: list[str]) -> list[str]:
    """剥 oneline hash 前缀后的消息段（匹配对象的钉法与 anchors.messages 一致）。"""
    return [line.split(" ", 1)[1] if " " in line else line for line in lines]


class TestFlagSurface:
    def test_flags_parse_to_repeatable_lists(self):
        args = build_parser().parse_args(
            ["git-log", "--grep", "a", "--grep", "b", "--exclude", "c"]
        )
        assert args.grep == ["a", "b"]
        assert args.exclude == ["c"]

    def test_flags_default_to_none(self):
        args = build_parser().parse_args(["git-log"])
        assert args.grep is None and args.exclude is None


class TestGrepFilter:
    def test_grep_keeps_matching_commits(self, tmp_path, monkeypatch, capsys):
        rc, lines = _run_git_log(monkeypatch, capsys, _seeded_root(tmp_path), "--grep", "write")
        assert rc == 0
        msgs = _messages(lines)
        assert len(msgs) == 2
        assert all(m.startswith("write ") for m in msgs)
        # 行本身仍是完整 oneline 形态（hash 前缀原样保留）
        assert all(" " in line and len(line.split(" ", 1)[0]) > 0 for line in lines)

    def test_grep_is_regex_not_plain_substring(self, tmp_path, monkeypatch, capsys):
        rc, lines = _run_git_log(
            monkeypatch, capsys, _seeded_root(tmp_path),
            "--grep", r"^feedback \S+ by agent-b: outcome=\S+ uses=\d+ conf=",
        )
        assert rc == 0
        # ^ 锚定消息段（hash 已剥）+ 转义字面量：纯 substring 语义下 "^feedback" 不可能命中
        # （模式含 outcome= 段：#53 契约变更后 feedback 消息模板的新形态）
        assert len(lines) == 1 and _messages(lines)[0].startswith("feedback ")

    def test_multiple_grep_is_or(self, tmp_path, monkeypatch, capsys):
        rc, lines = _run_git_log(
            monkeypatch, capsys, _seeded_root(tmp_path), "--grep", "init", "--grep", "revive"
        )
        assert rc == 0
        assert _messages(lines) == ["init compound-memory store"]

    def test_no_match_returns_empty_list(self, tmp_path, monkeypatch, capsys):
        rc, lines = _run_git_log(
            monkeypatch, capsys, _seeded_root(tmp_path), "--grep", "nosuchverb"
        )
        assert rc == 0 and lines == []


class TestExcludeFilter:
    def test_exclude_feedback(self, tmp_path, monkeypatch, capsys):
        """#31 裁决的主消费场景：高频 feedback 从审计窗口剔除。"""
        rc, lines = _run_git_log(
            monkeypatch, capsys, _seeded_root(tmp_path), "--exclude", "feedback"
        )
        assert rc == 0
        msgs = _messages(lines)
        assert len(msgs) == 3  # init + write*2
        assert not any(m.startswith("feedback ") for m in msgs)

    def test_exclude_is_regex(self, tmp_path, monkeypatch, capsys):
        rc, lines = _run_git_log(
            monkeypatch, capsys, _seeded_root(tmp_path), "--exclude", r"conf=0\.[0-9]+"
        )
        assert rc == 0
        assert not any(m.startswith("feedback ") for m in _messages(lines))

    def test_exclude_alone_keeps_everything(self, tmp_path, monkeypatch, capsys):
        """与 git 原生 --exclude（ref 排除，对消息不生效）的差异点：CLI 语义是消息反选。"""
        rc, lines = _run_git_log(
            monkeypatch, capsys, _seeded_root(tmp_path), "--exclude", "nothing-matches-this"
        )
        assert rc == 0 and len(lines) == 4


class TestCombined:
    def test_grep_then_exclude(self, tmp_path, monkeypatch, capsys):
        rc, lines = _run_git_log(
            monkeypatch, capsys,
            _seeded_root(tmp_path), "--grep", "write", "--exclude", "insight",
        )
        assert rc == 0
        msgs = _messages(lines)
        assert len(msgs) == 1
        assert msgs[0].startswith("write ") and "(fact/_shared)" in msgs[0]


class TestInvalidPattern:
    def test_invalid_regex_is_caller_error(self, tmp_path, monkeypatch, capsys):
        """非法正则走 CLI 调用方错误约定：stderr JSON + exit 2。"""
        monkeypatch.setattr(cli_mod, "auto_encoder", lambda: None)
        rc = cli_mod.main(
            ["--root", str(_seeded_root(tmp_path)), "git-log", "--limit", "10", "--grep", "["]
        )
        captured = capsys.readouterr()
        assert rc == 2
        assert "error" in json.loads(captured.err)
