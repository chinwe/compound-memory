"""dream --propose（#97 / ADR-0011）：默认不写包；显式 flag 才写 dream-apply 列表。

门槛：同 key ≥3、token 覆盖率、证据三计数之和、未裁决 contradiction 不进包。
无合格簇时文件是 []，只读报告照旧。
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
from compound_memory.storage.dream import PROPOSE_MIN_TOKEN_COVERAGE, _token_coverage
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


_STEM = "compound-memory 蒸馏管线把候选清单交给 agent 判断后合并同 key 版本"


def _similar(mark: str) -> str:
    return f"{_STEM}{mark}"


def _proposals_file(root: Path) -> Path:
    return root / "distill" / "dream-proposals.json"


def _write_similar(
    store: MemoryStore,
    mark: str,
    *,
    days: int,
    feedbacks: int = 1,
    confidence: float | None = None,
    key: str = "merge-key",
    mtype: str = "episode",
    valid_until: str | None = None,
) -> str:
    written = store.write(
        content=_similar(mark),
        type=mtype,
        source="agent-a",
        key=key,
        created=_days_ago(days),
        confidence=confidence,
        valid_until=valid_until,
    )
    for _ in range(feedbacks):
        store.feedback(written["id"], "agent-a")
    return written["id"]


def _seed_qualifying(store: MemoryStore) -> dict[str, str]:
    """三份近重复。证据最高的是较旧的「乙」，提议正文必须取它，不取更新的「甲」。"""
    return {
        "甲": _write_similar(store, "甲", days=1, feedbacks=1, confidence=0.9),
        "乙": _write_similar(store, "乙", days=5, feedbacks=2, confidence=0.5),
        "丙": _write_similar(store, "丙", days=3, feedbacks=1, confidence=0.5),
    }


class TestTokenCoverageFormula:
    def test_near_duplicate_passes_and_unrelated_is_zero(self):
        """ADR-0011 的覆盖率：仓库里那对近重复两个方向都过 0.5，无关正文是 0。"""
        left = "compound-memory 蒸馏管线把候选清单交给 agent 判断"
        right = "compound-memory 蒸馏管线把候选清单交给调用方判断"
        assert _token_coverage(left, right) >= PROPOSE_MIN_TOKEN_COVERAGE
        assert _token_coverage(right, left) >= PROPOSE_MIN_TOKEN_COVERAGE
        assert _token_coverage(left, "Kubernetes Pod 亲和性配置") == 0.0
        assert _token_coverage("", "anything") == 0.0


class TestDreamProposalsGates:
    def test_qualifying_cluster_is_applyable(self, store: MemoryStore):
        ids = _seed_qualifying(store)
        before_commits = _commit_count(store.root)

        pack = store.dream_proposals(ns="_shared")

        assert len(pack) == 1
        action = pack[0]
        assert set(action) == {"content", "type", "source_ids", "key"}
        assert action["content"] == _similar("乙")
        assert action["type"] == "episode"
        assert action["key"] == "merge-key"
        assert action["source_ids"] == [ids["乙"], ids["丙"], ids["甲"]]
        report = store.dream_apply(pack, source="agent-a", dry_run=True)
        assert report["skipped"] == []
        assert len(report["planned"]) == 1
        assert report["planned"][0]["signal"] in {"merge_with", "key_duplicates"}
        assert report["planned"][0]["source_ids"] == action["source_ids"]
        assert report["planned"][0]["content"] == action["content"]
        assert report["applied"] == []
        assert _commit_count(store.root) == before_commits

    def test_two_members_are_not_enough(self, store: MemoryStore):
        _write_similar(store, "甲", days=2)
        _write_similar(store, "乙", days=1)
        assert store.dream_proposals() == []

    def test_failure_counts_toward_evidence_total(self, store: MemoryStore):
        """M 是三计数之和，不是 success_count。三次 failure 也够。"""
        for days, mark in enumerate("甲乙丙", start=1):
            written = store.write(
                content=_similar(mark),
                type="episode",
                source="agent-a",
                key="merge-key",
                created=_days_ago(days),
            )
            store.feedback(written["id"], "agent-a", outcome="failure")
        pack = store.dream_proposals()
        assert len(pack) == 1
        assert len(pack[0]["source_ids"]) == 3

    def test_shared_key_does_not_inflate_unrelated_short_text(self, store: MemoryStore):
        """覆盖率只看正文。共享 key 不能让 alpha/beta/gamma 过线。"""
        for days, content in enumerate(("alpha", "beta", "gamma"), start=1):
            written = store.write(
                content=content,
                type="episode",
                source="agent-a",
                key="merge-key",
                created=_days_ago(days),
            )
            store.feedback(written["id"], "agent-a")
        assert store.dream_proposals() == []

    def test_zero_evidence_is_not_enough(self, store: MemoryStore):
        for days, mark in enumerate("甲乙丙", start=1):
            store.write(
                content=_similar(mark),
                type="episode",
                source="agent-a",
                key="merge-key",
                created=_days_ago(days),
            )
        assert store.dream_proposals() == []

    def test_divergent_contents_are_not_enough(self, store: MemoryStore):
        contents = [
            "Python GIL 全局解释器锁让同一时刻只有一个线程执行字节码",
            "Redis 持久化可以选 RDB 快照或者 AOF 追加日志两种方式",
            "Kubernetes 把 Pod 调度到节点时看亲和性与污点容忍",
        ]
        for days, content in enumerate(contents, start=1):
            written = store.write(
                content=content,
                type="episode",
                source="agent-a",
                key="merge-key",
                created=_days_ago(days),
            )
            store.feedback(written["id"], "agent-a")
        assert store.dream_proposals() == []

    def test_expired_memories_are_outside_the_population(self, store: MemoryStore):
        for days, mark in enumerate("甲乙丙", start=1):
            _write_similar(store, mark, days=days, valid_until=_days_ago(1))
        assert store.dream_proposals() == []

    def test_key_conflict_rows_do_not_block_a_pack(self, store: MemoryStore):
        """同 key fact 内容冲突会进 review 队列，但那不是 pending contradiction。"""
        for days, mark in enumerate("甲乙丙", start=1):
            _write_similar(store, mark, days=days, mtype="fact", key="policy")
        assert store.review_queue()
        assert store._review_queue.pending_contradictions() == set()
        pack = store.dream_proposals()
        assert len(pack) == 1
        assert pack[0]["type"] == "fact"
        assert pack[0]["key"] == "policy"
        assert len(pack[0]["source_ids"]) == 3

    def test_pending_contradiction_never_enters_a_pack(self, store: MemoryStore):
        ids = [
            _write_similar(store, mark, days=4 - index)
            for index, mark in enumerate("甲乙丙丁")
        ]
        frozen = ids[3]
        store.feedback(frozen, "agent-a", outcome="contradiction")
        assert frozen in store._review_queue.pending_contradictions()

        pack = store.dream_proposals()

        assert len(pack) == 1
        assert pack[0]["source_ids"] == ids[:3]
        assert frozen not in pack[0]["source_ids"]
        report = store.dream_apply(pack, source="agent-a", dry_run=True)
        assert report["skipped"] == []
        assert frozen not in report["planned"][0]["source_ids"]

    def test_private_ns_requires_owner(self, store: MemoryStore):
        store.write(
            content=_similar("甲"),
            type="episode",
            source="agent-zcode",
            ns="agent-zcode",
            key="merge-key",
        )
        with pytest.raises(PermissionError):
            store.dream_proposals(ns="agent-zcode")
        assert store.dream_proposals(ns="agent-zcode", reader="zcode") == []


class TestDreamProposeCli:
    def test_no_flag_does_not_write_a_pack(self, store: MemoryStore, capsys: pytest.CaptureFixture[str]):
        """合格簇就在库里：没有 --propose 时报告形状与文件字节都不变。"""
        _seed_qualifying(store)
        before_files = _payload_files(store.root)
        before_commits = _commit_count(store.root)

        assert cli_main(["--root", str(store.root), "dream", "--ns", "_shared"]) == 0
        captured = capsys.readouterr()
        report = json.loads(captured.out)

        assert set(report) == {
            "ns",
            "dry_run",
            "summary",
            "distill",
            "decay_signals",
            "stats_before",
            "stats_after",
        }
        assert report["dry_run"] is True
        assert report["stats_before"] == report["stats_after"]
        assert captured.err.strip() == report["summary"]
        assert "dream proposals" not in captured.err
        assert not _proposals_file(store.root).exists()
        assert _payload_files(store.root) == before_files
        assert _commit_count(store.root) == before_commits

    def test_propose_writes_applyable_pack(self, store: MemoryStore, capsys: pytest.CaptureFixture[str]):
        ids = _seed_qualifying(store)
        before_files = _payload_files(store.root)
        before_commits = _commit_count(store.root)
        root_arg = str(store.root)

        assert cli_main(["--root", root_arg, "dream", "--ns", "_shared", "--propose"]) == 0
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        path = _proposals_file(store.root)
        pack = json.loads(path.read_text(encoding="utf-8"))

        assert report["dry_run"] is True
        assert "proposals" not in report
        assert set(report) == {
            "ns",
            "dry_run",
            "summary",
            "distill",
            "decay_signals",
            "stats_before",
            "stats_after",
        }
        assert report["summary"] in captured.err
        assert f"dream proposals ns=_shared: count=1 path={path}" in captured.err
        assert pack[0]["content"] == _similar("乙")
        assert pack[0]["source_ids"] == [ids["乙"], ids["丙"], ids["甲"]]
        after = _payload_files(store.root)
        assert set(after) - set(before_files) == {"distill/dream-proposals.json"}
        assert all(after[name] == before_files[name] for name in before_files)
        assert _commit_count(store.root) == before_commits

        assert cli_main(
            [
                "--root",
                root_arg,
                "dream-apply",
                "--proposals",
                str(path),
                "--source",
                "agent-a",
                "--dry-run",
            ]
        ) == 0
        applied = json.loads(capsys.readouterr().out)
        assert applied["skipped"] == []
        assert applied["planned"][0]["source_ids"] == pack[0]["source_ids"]
        assert applied["applied"] == []
        assert _commit_count(store.root) == before_commits

    def test_propose_with_nothing_qualified_writes_empty_list(
        self, store: MemoryStore, capsys: pytest.CaptureFixture[str]
    ):
        _write_similar(store, "甲", days=2)
        _write_similar(store, "乙", days=1)
        before_commits = _commit_count(store.root)

        assert cli_main(["--root", str(store.root), "dream", "--propose"]) == 0
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        path = _proposals_file(store.root)

        assert report["dry_run"] is True
        assert path.read_text(encoding="utf-8") == "[]\n"
        assert f"dream proposals ns=_shared: count=0 empty path={path}" in captured.err
        assert _commit_count(store.root) == before_commits

    def test_propose_file_omits_pending_contradiction(
        self, store: MemoryStore, capsys: pytest.CaptureFixture[str]
    ):
        ids = [
            _write_similar(store, mark, days=4 - index)
            for index, mark in enumerate("甲乙丙丁")
        ]
        store.feedback(ids[3], "agent-a", outcome="contradiction")

        assert cli_main(["--root", str(store.root), "dream", "--propose"]) == 0
        capsys.readouterr()
        pack = json.loads(_proposals_file(store.root).read_text(encoding="utf-8"))

        assert pack[0]["source_ids"] == ids[:3]
        assert ids[3] not in {sid for action in pack for sid in action["source_ids"]}

    def test_help_mentions_propose_and_not_auto(self):
        action = next(
            item for item in build_parser()._actions if isinstance(item, argparse._SubParsersAction)
        )
        text = action.choices["dream"].format_help().lower()
        assert "--propose" in text
        assert "--auto" not in text
        assert "--apply" not in text
        assert "read-only" in text
        assert "dream-apply" in text
