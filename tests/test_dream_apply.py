"""dream-apply 显式半自动落库（#72）。

钉住六项裁决：独立子命令；调用方先给正文；possible_dup_of 只报告；
无 --include-forget；无 git-revert 辅助；定时脚本只调只读 dream。
落库只经 distill_apply。dream 的报告形状保持 v0.5.4。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from compound_memory.cli import build_parser
from compound_memory.cli import main as cli_main
from compound_memory.storage import MemoryStore
from compound_memory.storage.dream import APPLY_SIGNALS
from conftest import CLOCK_DATE


def _days_ago(n: int) -> str:
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


def _commit_count(root: Path) -> int:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


def _payload_files(root: Path) -> dict[str, bytes]:
    payload: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if ".git" in rel.parts:
            continue
        payload[rel.as_posix()] = path.read_bytes()
    return payload


def _code(text: str) -> str:
    """去掉 # 注释，用来确认脚本真正执行的命令。"""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _key_pair(store: MemoryStore) -> dict[str, str]:
    """uses=0 的同 key 双版本：进 key_duplicates，进不了 merge_with（活性门）。"""
    older = store.write(
        content="proj-x 的旧版结论",
        type="fact",
        source="agent-a",
        key="proj-x",
        created=_days_ago(40),
    )
    newer = store.write(
        content="proj-x 的新版结论",
        type="fact",
        source="agent-a",
        key="proj-x",
        created=_days_ago(1),
    )
    return {"older": older["id"], "newer": newer["id"]}


def _merge_pair(store: MemoryStore) -> dict[str, str]:
    """活性门内的同 key 双版本：merge_with 强信号。"""
    first = store.write(content="deploy 流程记录甲", type="episode", source="agent-a", key="deploy")
    second = store.write(content="deploy 流程记录乙", type="episode", source="agent-a", key="deploy")
    store.feedback(first["id"], "agent-a")
    store.feedback(second["id"], "agent-a")
    return {"first": first["id"], "second": second["id"]}


def _action(source_ids: list[str], **overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "content": "调用方给出的产物正文",
        "type": "insight",
        "source_ids": source_ids,
    }
    body.update(overrides)
    return body


class TestApplySignalAllowlist:
    def test_possible_dup_is_not_an_apply_signal(self):
        assert APPLY_SIGNALS == ("merge_with", "key_duplicates", "promotion_candidate")


class TestDreamApplyGates:
    def test_dry_run_plans_key_duplicates_without_writes(self, store: MemoryStore):
        ids = _key_pair(store)
        before_files = _payload_files(store.root)
        before_commits = _commit_count(store.root)

        report = store.dream_apply(
            [_action([ids["older"], ids["newer"]], type="fact", key="proj-x")],
            source="agent-a",
            dry_run=True,
        )

        assert report["dry_run"] is True
        assert report["applied"] == []
        assert report["skipped"] == []
        assert report["stats_before"] == report["stats_after"]
        assert report["summary"] == "dream-apply ns=_shared dry-run: planned=1 applied=0 skipped=0"
        planned = report["planned"]
        assert len(planned) == 1
        assert planned[0]["signal"] == "key_duplicates"
        assert planned[0]["source_ids"] == [ids["older"], ids["newer"]]
        assert planned[0]["content"] == "调用方给出的产物正文"
        assert "result" not in planned[0]
        assert _commit_count(store.root) == before_commits
        assert _payload_files(store.root) == before_files

    def test_key_duplicates_apply_writes_through_distill_apply(self, store: MemoryStore):
        ids = _key_pair(store)
        before_commits = _commit_count(store.root)

        report = store.dream_apply(
            [_action([ids["older"], ids["newer"]], type="fact", key="proj-x-merged")],
            source="agent-a",
        )

        assert report["dry_run"] is False
        assert report["planned"] == []
        assert report["skipped"] == []
        assert report["summary"] == "dream-apply ns=_shared apply: planned=0 applied=1 skipped=0"
        applied = report["applied"][0]
        assert applied["signal"] == "key_duplicates"
        result = applied["result"]
        assert result["found"] is True
        assert result["origin"] == "distillation"
        assert result["archived_sources"] == [ids["older"], ids["newer"]]
        assert store.get(result["id"])["origin"] == "distillation"
        assert store.get(ids["older"])["archived"] is True
        assert store.get(ids["newer"])["archived"] is True
        assert _commit_count(store.root) == before_commits + 1
        assert "distill apply" in store.git_log(1)[0]
        assert report["stats_before"] != report["stats_after"]

    def test_merge_with_signal_when_both_pass_the_activity_gate(self, store: MemoryStore):
        ids = _merge_pair(store)
        report = store.dream_apply(
            [_action([ids["first"], ids["second"]])],
            source="agent-a",
            dry_run=True,
        )
        assert report["planned"][0]["signal"] == "merge_with"
        assert report["applied"] == []

    def test_spy_invokes_only_distill_apply(self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch):
        ids = _key_pair(store)
        calls: list[dict[str, object]] = []

        def spy(**kwargs: object) -> dict[str, object]:
            calls.append(kwargs)
            return {"found": True, "id": "spy-product", "origin": "distillation", "archived_sources": kwargs["source_ids"]}

        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("dream-apply invoked a write verb other than distill_apply")

        monkeypatch.setattr(store, "distill_apply", spy)
        for name in ("write", "feedback", "decay_sweep", "forget", "revive", "_archive", "_commit", "_save", "batch"):
            monkeypatch.setattr(store, name, boom)

        report = store.dream_apply(
            [_action([ids["older"], ids["newer"]], type="fact", key="proj-x-merged")],
            source="agent-a",
        )

        assert len(calls) == 1
        assert calls[0]["content"] == "调用方给出的产物正文"
        assert calls[0]["source_ids"] == [ids["older"], ids["newer"]]
        assert calls[0]["source"] == "agent-a"
        assert report["applied"][0]["result"]["origin"] == "distillation"
        assert report["applied"][0]["signal"] == "key_duplicates"

    def test_dry_run_does_not_call_distill_apply(self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch):
        ids = _key_pair(store)

        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("dry-run called distill_apply")

        monkeypatch.setattr(store, "distill_apply", boom)
        report = store.dream_apply(
            [_action([ids["older"], ids["newer"]], type="fact")],
            source="agent-a",
            dry_run=True,
        )
        assert report["planned"][0]["signal"] == "key_duplicates"
        assert report["applied"] == []

    def test_possible_dup_of_is_report_only(self, store: MemoryStore):
        dup_a = store.write(
            content="compound-memory 蒸馏管线把候选清单交给 agent 判断",
            type="episode",
            source="agent-a",
        )
        dup_b = store.write(
            content="compound-memory 蒸馏管线把候选清单交给调用方判断",
            type="episode",
            source="agent-a",
        )
        for title in (
            "Python GIL 全局解释器锁行为",
            "Go goroutine 并发模型要点",
            "Redis 持久化 RDB 与 AOF 差异",
            "Kubernetes Pod 亲和性配置",
            "HTTP ETag 协商缓存机制",
            "Vercel Serverless 函数超时",
        ):
            store.write(content=title, type="episode", source="agent-a")
        before_files = _payload_files(store.root)
        before_commits = _commit_count(store.root)

        report = store.dream_apply(
            [_action([dup_a["id"], dup_b["id"]])],
            source="agent-a",
            min_uses=0,
        )

        assert report["applied"] == []
        assert report["planned"] == []
        assert len(report["skipped"]) == 1
        assert report["skipped"][0]["source_ids"] == [dup_a["id"], dup_b["id"]]
        assert report["skipped"][0]["reason"] == "possible_dup_of is report-only"
        assert report["stats_before"] == report["stats_after"]
        assert store.get(dup_a["id"])["archived"] is False
        assert store.get(dup_b["id"])["archived"] is False
        assert _commit_count(store.root) == before_commits
        assert _payload_files(store.root) == before_files

    def test_promotion_below_threshold_refused(self, store: MemoryStore):
        cold = store.write(content="只被用过一次的经验", type="episode", source="agent-a")
        store.feedback(cold["id"], "agent-a")
        before_commits = _commit_count(store.root)

        report = store.dream_apply(
            [_action([cold["id"]], promote=True)],
            source="agent-a",
        )

        assert report["applied"] == []
        assert report["skipped"][0]["reason"] == "below promotion threshold"
        assert report["skipped"][0]["source_ids"] == [cold["id"]]
        assert store.get(cold["id"])["archived"] is False
        assert _commit_count(store.root) == before_commits

    def test_promotion_requires_explicit_opt_in(self, store: MemoryStore):
        hot = store.write(content="反复验证的部署经验", type="episode", source="agent-a")
        for agent in ("a", "b", "c", "d", "e"):
            store.feedback(hot["id"], agent)
        before_files = _payload_files(store.root)

        report = store.dream_apply([_action([hot["id"]])], source="agent-a")

        assert report["applied"] == []
        assert report["skipped"][0]["reason"] == "promotion_candidate requires explicit opt-in"
        assert _payload_files(store.root) == before_files

    def test_explicit_promotion_applies_through_distill_apply(self, store: MemoryStore):
        hot = store.write(content="反复验证的部署经验", type="episode", source="agent-a")
        for agent in ("a", "b", "c", "d", "e"):
            store.feedback(hot["id"], agent)

        report = store.dream_apply(
            [_action([hot["id"]], promote=True, content="晋升后的部署洞察")],
            source="agent-a",
        )

        applied = report["applied"][0]
        assert applied["signal"] == "promotion_candidate"
        assert applied["result"]["origin"] == "distillation"
        assert applied["result"]["found"] is True
        assert applied["result"]["archived_sources"] == [hot["id"]]
        assert store.get(hot["id"])["archived"] is True
        assert "distill apply" in store.git_log(1)[0]

    def test_single_member_of_a_key_group_is_not_applied(self, store: MemoryStore):
        ids = _key_pair(store)
        before_files = _payload_files(store.root)

        report = store.dream_apply(
            [_action([ids["older"]], type="fact", content="只归档旧版、不给合并包")],
            source="agent-a",
        )

        assert report["applied"] == []
        assert report["skipped"][0]["reason"] == "no strong signal"
        assert store.get(ids["older"])["archived"] is False
        assert store.get(ids["newer"])["archived"] is False
        assert _payload_files(store.root) == before_files

    def test_mixed_package_applies_strong_signal_and_lists_the_skip(self, store: MemoryStore):
        ids = _key_pair(store)
        dup_a = store.write(content="完全不同的主题甲 数据库迁移", type="episode", source="agent-a")
        dup_b = store.write(content="完全不同的主题乙 前端配色", type="episode", source="agent-a")
        store.feedback(dup_a["id"], "agent-a")
        store.feedback(dup_b["id"], "agent-a")

        report = store.dream_apply(
            [
                _action([ids["older"], ids["newer"]], type="fact", key="proj-x-merged"),
                _action([dup_a["id"], dup_b["id"]]),
            ],
            source="agent-a",
        )

        assert len(report["applied"]) == 1
        assert report["applied"][0]["signal"] == "key_duplicates"
        assert report["applied"][0]["result"]["origin"] == "distillation"
        assert len(report["skipped"]) == 1
        assert report["skipped"][0]["source_ids"] == [dup_a["id"], dup_b["id"]]
        assert report["skipped"][0]["reason"] == "no strong signal"
        assert store.get(dup_a["id"])["archived"] is False
        assert store.get(ids["older"])["archived"] is True

    def test_missing_content_writes_nothing(self, store: MemoryStore):
        ids = _key_pair(store)
        before_files = _payload_files(store.root)
        before_commits = _commit_count(store.root)
        package = [
            _action([ids["older"], ids["newer"]], type="fact", key="proj-x-merged"),
            {"type": "fact", "source_ids": [ids["older"], ids["newer"]]},
        ]

        with pytest.raises(ValueError, match="content"):
            store.dream_apply(package, source="agent-a")

        assert store.get(ids["older"])["archived"] is False
        assert _commit_count(store.root) == before_commits
        assert _payload_files(store.root) == before_files

    def test_unknown_forget_field_writes_nothing(self, store: MemoryStore):
        ids = _key_pair(store)
        before_commits = _commit_count(store.root)
        package = [_action([ids["older"], ids["newer"]], type="fact", forget=True)]

        with pytest.raises(ValueError, match="unknown fields"):
            store.dream_apply(package, source="agent-a")

        assert _commit_count(store.root) == before_commits
        assert store.get(ids["older"])["archived"] is False

    def test_overlapping_admitted_sources_write_nothing(self, store: MemoryStore):
        ids = _key_pair(store)
        before_files = _payload_files(store.root)

        with pytest.raises(ValueError, match="reuse source ids"):
            store.dream_apply(
                [
                    _action([ids["older"], ids["newer"]], type="fact", key="proj-x-merged"),
                    _action([ids["newer"], ids["older"]], type="fact", content="第二份产物"),
                ],
                source="agent-a",
                dry_run=True,
            )

        assert _payload_files(store.root) == before_files

    def test_private_ns_requires_owner_before_write(self, store: MemoryStore):
        older = store.write(
            content="私有旧版",
            type="fact",
            source="agent-zcode",
            ns="agent-zcode",
            key="priv-key",
        )
        newer = store.write(
            content="私有新版",
            type="fact",
            source="agent-zcode",
            ns="agent-zcode",
            key="priv-key",
        )
        action = _action([older["id"], newer["id"]], type="fact", key="priv-merged")
        before_files = _payload_files(store.root)

        with pytest.raises(PermissionError):
            store.dream_apply([action], source="agent-zcode", ns="agent-zcode", dry_run=True)
        with pytest.raises(PermissionError):
            store.dream_apply(
                [action],
                source="agent-a",
                ns="agent-zcode",
                reader="zcode",
                dry_run=True,
            )

        report = store.dream_apply(
            [action],
            source="agent-zcode",
            ns="agent-zcode",
            reader="zcode",
            dry_run=True,
        )
        assert report["planned"][0]["signal"] == "key_duplicates"
        assert _payload_files(store.root) == before_files

    def test_dream_report_shape_stays_read_only(self, store: MemoryStore):
        ids = _key_pair(store)
        before_files = _payload_files(store.root)
        before_commits = _commit_count(store.root)

        report = store.dream(ns="_shared")

        assert report["dry_run"] is True
        assert set(report) == {
            "ns",
            "dry_run",
            "summary",
            "distill",
            "decay_signals",
            "stats_before",
            "stats_after",
        }
        assert report["stats_before"] == report["stats_after"]
        groups = report["distill"]["key_duplicates"]
        assert {member["id"] for member in groups[0]["members"]} == {ids["older"], ids["newer"]}
        assert _commit_count(store.root) == before_commits
        assert _payload_files(store.root) == before_files


class TestDreamApplyCli:
    def test_dry_run_then_apply(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        root = tmp_path / "dream-apply-root"
        root_arg = str(root)

        def run(*argv: str) -> dict:
            assert cli_main(["--root", root_arg, *argv]) == 0
            captured = capsys.readouterr()
            return json.loads(captured.out)

        older = run("write", "proj-x 旧版", "fact", "agent-cli", "--key", "proj-x")
        newer = run("write", "proj-x 新版", "fact", "agent-cli", "--key", "proj-x")
        proposals = tmp_path / "proposals.json"
        proposals.write_text(
            json.dumps(
                [
                    {
                        "content": "合并后的 proj-x 结论",
                        "type": "fact",
                        "source_ids": [older["id"], newer["id"]],
                        "key": "proj-x-merged",
                    }
                ]
            ),
            encoding="utf-8",
        )
        before_files = _payload_files(root)
        before_commits = _commit_count(root)

        assert cli_main(["--root", root_arg, "dream", "--ns", "_shared"]) == 0
        dream_out = capsys.readouterr()
        dream_report = json.loads(dream_out.out)
        assert dream_report["dry_run"] is True
        assert dream_out.err.strip() == dream_report["summary"]
        assert _commit_count(root) == before_commits
        assert _payload_files(root) == before_files

        assert cli_main(
            ["--root", root_arg, "dream-apply", "--proposals", str(proposals), "--source", "agent-cli", "--dry-run"]
        ) == 0
        dry_captured = capsys.readouterr()
        dry = json.loads(dry_captured.out)
        assert dry["dry_run"] is True
        assert dry["planned"][0]["signal"] == "key_duplicates"
        assert dry["applied"] == []
        assert dry_captured.err.strip() == dry["summary"]
        assert _commit_count(root) == before_commits
        assert _payload_files(root) == before_files

        assert cli_main(
            ["--root", root_arg, "dream-apply", "--proposals", str(proposals), "--source", "agent-cli"]
        ) == 0
        applied = json.loads(capsys.readouterr().out)
        assert applied["dry_run"] is False
        assert applied["applied"][0]["result"]["origin"] == "distillation"
        assert applied["applied"][0]["result"]["found"] is True
        assert _commit_count(root) == before_commits + 1

    def test_under_threshold_exit_is_success_and_lists_the_skip(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        root = tmp_path / "skip-root"
        written = cli_main(["--root", str(root), "write", "一次经验", "episode", "agent-cli"])
        assert written == 0
        created = json.loads(capsys.readouterr().out)
        created_id = created["id"]
        cli_main(["--root", str(root), "feedback", created_id, "agent-cli"])
        capsys.readouterr()
        proposals = tmp_path / "promote.json"
        proposals.write_text(
            json.dumps([{"content": "还不够格的洞察", "type": "insight", "source_ids": [created_id], "promote": True}]),
            encoding="utf-8",
        )
        before_commits = _commit_count(root)

        assert cli_main(
            ["--root", str(root), "dream-apply", "--proposals", str(proposals), "--source", "agent-cli"]
        ) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["applied"] == []
        assert report["skipped"][0]["reason"] == "below promotion threshold"
        assert _commit_count(root) == before_commits

    def test_missing_content_is_a_caller_error(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        root = tmp_path / "bad-root"
        proposals = tmp_path / "bad.json"
        proposals.write_text(json.dumps([{"type": "fact", "source_ids": ["a", "b"]}]), encoding="utf-8")
        assert cli_main(
            ["--root", str(root), "dream-apply", "--proposals", str(proposals), "--source", "agent-cli"]
        ) == 2
        err = json.loads(capsys.readouterr().err)
        assert "content" in err["error"]

    def test_dream_apply_flag_is_rejected(self):
        with pytest.raises(SystemExit) as caught:
            cli_main(["dream", "--apply"])
        assert caught.value.code == 2
        with pytest.raises(SystemExit) as forget_flag:
            cli_main(["dream-apply", "--proposals", "p.json", "--source", "agent-a", "--include-forget"])
        assert forget_flag.value.code == 2

    def test_help_describes_gates_and_dry_run(self):
        action = next(
            item for item in build_parser()._actions if isinstance(item, argparse._SubParsersAction)
        )
        text = action.choices["dream-apply"].format_help().lower()
        assert "dry-run" in text
        assert "merge_with" in text
        assert "key_duplicates" in text
        assert "promotion_candidate" in text
        assert "possible_dup_of" in text
        assert "distill-apply" in text
        assert "forget" in text
        assert "git-revert" in text
        dream_help = action.choices["dream"].format_help().lower()
        assert "read-only" in text or "read-only" in dream_help
        assert "--apply" not in dream_help


class TestTimersStayReadOnly:
    def test_prepare_scripts_invoke_dream_only(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        sh = (scripts / "distill-prepare.sh").read_text(encoding="utf-8")
        code = _code(sh)
        assert "dream --ns _shared" in code
        assert "dream-apply" not in code
        for path in scripts.glob("*distill-prepare*"):
            assert "dream-apply" not in _code(path.read_text(encoding="utf-8"))
        daily = _code((scripts / "daily-maintain.sh").read_text(encoding="utf-8"))
        assert "compound-memory maintain" in daily
        assert "dream-apply" not in daily
        assert "--propose" not in daily
