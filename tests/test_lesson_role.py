"""教训语义（#78）：lesson_role + 链接前缀 + 报告段。

不新增记忆类型，不扩 suggested_action，不放宽 dream-apply。
负向段与矛盾对只报告。借用教训语义，不搬因果引擎。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from compound_memory.cli import main as cli_main
from compound_memory.storage import MemoryStore
from compound_memory.storage.distill import SUGGESTED_ACTIONS, parse_lesson_link
from compound_memory.storage.dream import APPLY_SIGNALS, REASON_NO_STRONG_SIGNAL
from compound_memory.storage.files import parse
from conftest import CLOCK_DATE

_SALIENCE_KEYS = {
    "id",
    "score",
    "lesson_role",
    "confidence",
    "failure_count",
    "contradiction_count",
    "signals",
}
_PAIR_KEYS = {"ids", "reason", "type", "key"}


def _days_ago(n: int) -> str:
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


def _pair(left: str, right: str, reason: str, mtype: str | None, key: str | None) -> dict:
    ids = sorted((left, right))
    return {"ids": ids, "reason": reason, "type": mtype, "key": key}


class TestLessonLinkPrefix:
    @pytest.mark.parametrize(
        ("link", "expected"),
        [
            ("avoid:20261015_abcdef", ("avoid", "20261015_abcdef")),
            ("fixes:retry-budget", ("fixes", "retry-budget")),
            ("contradicts:20261015_abcdef", ("contradicts", "20261015_abcdef")),
            ("contradicts: retry-budget", ("contradicts", "retry-budget")),
            ("see contradicts:foo", None),
            ("contradicts:", None),
            ("contradicts:two words", None),
            ("AVOID:foo", None),
            ("plain-id", None),
        ],
    )
    def test_only_three_prefixes(self, link: str, expected: tuple[str, str] | None):
        assert parse_lesson_link(link) == expected


class TestLessonRoleField:
    def test_round_trip_and_omit(self, store: MemoryStore):
        avoid = store.write(
            "不要盲重试",
            type="decision",
            source="agent-a",
            lesson_role="avoid",
        )
        lesson = store.write(
            "重试前先看错误码",
            type="insight",
            source="agent-a",
            lesson_role="lesson",
        )
        plain = store.write("普通事实", type="fact", source="agent-a")

        avoid_path = store.ns_root / "_shared" / "decision" / f"{avoid['id']}.md"
        plain_path = store.ns_root / "_shared" / "fact" / f"{plain['id']}.md"
        assert "lesson_role: avoid" in avoid_path.read_text(encoding="utf-8")
        assert "lesson_role" not in plain_path.read_text(encoding="utf-8")
        assert store.get(avoid["id"])["lesson_role"] == "avoid"
        assert store.get(lesson["id"])["lesson_role"] == "lesson"
        assert store.get(plain["id"])["lesson_role"] is None

    @pytest.mark.parametrize("bad", ["bogus", "", "Lesson", "avoid-this"])
    def test_bad_role_is_caller_error(self, store: MemoryStore, bad: str):
        with pytest.raises(ValueError, match="lesson_role"):
            store.write("x", type="insight", source="agent-a", lesson_role=bad)

    def test_bad_frontmatter_is_unparseable(self, store: MemoryStore, caplog: pytest.LogCaptureFixture):
        folder = store.ns_root / "_shared" / "fact"
        folder.mkdir(parents=True, exist_ok=True)
        bad = folder / "20260101_badrol.md"
        bad.write_text(
            "---\n"
            "id: 20260101_badrol\n"
            "ns: _shared\n"
            "type: fact\n"
            "source: agent-a\n"
            "created: 2026-10-01\n"
            "lesson_role: nope\n"
            "---\n\n"
            "bad role\n",
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="lesson_role"):
            parse(bad)
        with caplog.at_level("WARNING"):
            plan = store.distill_plan(min_uses=0)
        assert all(row["id"] != "20260101_badrol" for row in plan["negative_salience"])
        assert "unparseable" in caplog.text


class TestLessonReport:
    def test_sections_pin_shape_and_do_not_widen_apply(self, store: MemoryStore):
        """夹具：一条 lesson、一条 avoid、一条高 failure_count、同 key 矛盾与 contradicts: 对。

        默认 dream / distill-plan（活性门开着）仍要打出这两段。
        dream-apply 只放行原有同 key 强信号，矛盾对与失败记忆不入选。
        """
        lesson = store.write(
            "重试前先看错误码",
            type="insight",
            source="agent-a",
            key="retry-read-error",
            lesson_role="lesson",
        )
        avoid = store.write(
            "不要盲重试",
            type="decision",
            source="agent-a",
            key="no-blind-retry",
            lesson_role="avoid",
            links=[f"avoid:{lesson['id']}", f"contradicts:{lesson['id']}"],
        )
        old = store.write(
            "超时一律重试三次",
            type="insight",
            source="agent-a",
            key="retry-budget",
            created=_days_ago(2),
        )
        new = store.write(
            "超时先区分幂等再重试",
            type="insight",
            source="agent-a",
            key="retry-budget",
            created=_days_ago(1),
            links=["contradicts:retry-budget"],
        )
        failed = store.write(
            "把临时端口写进配置",
            type="fact",
            source="agent-a",
            key="temp-port",
        )
        for _ in range(3):
            store.feedback(failed["id"], "agent-a", outcome="failure")
        prefix = store.write(
            "普通事实",
            type="fact",
            source="agent-a",
            key="avoid-looks-like-a-prefix",
        )
        fixer = store.write(
            "改用幂等键",
            type="insight",
            source="agent-a",
            links=[f"fixes:{old['id']}"],
        )
        prose = store.write(
            "笔记里提到 contradicts 这个词",
            type="episode",
            source="agent-a",
            links=["contradicts the old note"],
        )
        expired = store.write(
            "过期的禁止",
            type="decision",
            source="agent-a",
            lesson_role="avoid",
            valid_until=_days_ago(1),
        )
        private = store.write(
            "私有禁止",
            type="decision",
            source="agent-zcode",
            ns="agent-zcode",
            lesson_role="avoid",
        )

        plan = store.distill_plan()
        report = store.dream()

        assert report["distill"]["negative_salience"] == plan["negative_salience"]
        assert report["distill"]["contradiction_pairs"] == plan["contradiction_pairs"]
        assert "negative=" in report["summary"]
        assert "contradictions=" in report["summary"]
        assert {row["suggested_action"] for row in plan["suggested_actions"]} <= set(SUGGESTED_ACTIONS)
        assert "avoid" not in {row["suggested_action"] for row in plan["suggested_actions"]}
        assert SUGGESTED_ACTIONS == ("merge", "promote", "review")
        assert APPLY_SIGNALS == ("merge_with", "key_duplicates", "promotion_candidate")

        for row in plan["negative_salience"]:
            assert set(row) == _SALIENCE_KEYS
        for row in plan["contradiction_pairs"]:
            assert set(row) == _PAIR_KEYS

        by_id = {row["id"]: row for row in plan["negative_salience"]}
        assert failed["id"] in by_id
        failed_row = by_id[failed["id"]]
        assert failed_row["failure_count"] == 3
        assert failed_row["contradiction_count"] == 0
        assert failed_row["confidence"] == 0.05
        assert failed_row["lesson_role"] is None
        assert failed_row["signals"] == ["failure_count", "low_confidence"]
        assert failed_row["score"] == 3.5

        avoid_row = by_id[avoid["id"]]
        assert avoid_row["lesson_role"] == "avoid"
        assert avoid_row["signals"] == ["avoid", "avoid_link", "contradicts"]
        assert avoid_row["score"] == 3.0
        assert avoid_row["failure_count"] == 0

        for cid in (lesson["id"], old["id"], new["id"]):
            assert by_id[cid]["signals"] == ["contradicts"]
            assert by_id[cid]["score"] == 1.0

        hidden = {prefix["id"], fixer["id"], prose["id"], expired["id"], private["id"]}
        assert hidden.isdisjoint(by_id)
        assert [row["id"] for row in plan["negative_salience"]] == [
            failed["id"],
            avoid["id"],
            *sorted((lesson["id"], old["id"], new["id"])),
        ]

        expected_pairs = sorted(
            [
                _pair(old["id"], new["id"], "contradicts", "insight", "retry-budget"),
                _pair(old["id"], new["id"], "same_key", "insight", "retry-budget"),
                _pair(avoid["id"], lesson["id"], "contradicts", None, None),
            ],
            key=lambda row: (row["ids"], row["reason"]),
        )
        assert plan["contradiction_pairs"] == expected_pairs

        private_report = store.dream(ns="agent-zcode", reader="zcode")
        assert private["id"] in {
            row["id"] for row in private_report["distill"]["negative_salience"]
        }

        apply_report = store.dream_apply(
            [
                {
                    "content": "纠正后的重试教训",
                    "type": "insight",
                    "source_ids": [avoid["id"], lesson["id"]],
                },
                {
                    "content": "不该自动落的失败事实",
                    "type": "fact",
                    "source_ids": [failed["id"]],
                },
                {
                    "content": "同 key 仍按旧门槛",
                    "type": "insight",
                    "source_ids": [old["id"], new["id"]],
                    "key": "retry-budget",
                },
            ],
            source="agent-a",
            dry_run=True,
        )
        assert [row["source_ids"] for row in apply_report["planned"]] == [[old["id"], new["id"]]]
        assert apply_report["planned"][0]["signal"] == "key_duplicates"
        assert apply_report["applied"] == []
        assert [row["source_ids"] for row in apply_report["skipped"]] == [
            [avoid["id"], lesson["id"]],
            [failed["id"]],
        ]
        assert {row["reason"] for row in apply_report["skipped"]} == {REASON_NO_STRONG_SIGNAL}

    def test_negative_only_apply_does_not_call_distill_apply(
        self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
    ):
        left = store.write("甲结论", type="insight", source="agent-a", key="alpha")
        right = store.write(
            "乙结论",
            type="decision",
            source="agent-a",
            lesson_role="avoid",
            links=[f"contradicts:{left['id']}"],
        )

        def boom(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("dream-apply widened onto a contradicts pair")

        monkeypatch.setattr(store, "distill_apply", boom)
        report = store.dream_apply(
            [
                {
                    "content": "不该落库",
                    "type": "insight",
                    "source_ids": [left["id"], right["id"]],
                }
            ],
            source="agent-a",
        )
        assert report["planned"] == []
        assert report["applied"] == []
        assert report["skipped"][0]["reason"] == REASON_NO_STRONG_SIGNAL

    def test_plain_lesson_is_not_negative(self, store: MemoryStore):
        store.write("稳态教训", type="insight", source="agent-a", lesson_role="lesson")
        plan = store.distill_plan()
        assert plan["negative_salience"] == []
        assert plan["contradiction_pairs"] == []

    def test_same_content_same_key_is_not_a_contradiction(self, store: MemoryStore):
        store.write("同一正文", type="fact", source="agent-a", key="same-body")
        store.write("同一正文", type="fact", source="agent-a", key="same-body")
        plan = store.distill_plan(min_uses=0)
        assert len(plan["key_duplicates"]) == 1
        assert plan["contradiction_pairs"] == []
        assert plan["negative_salience"] == []

    def test_old_avoid_ignores_window_and_activity_gate(self, store: MemoryStore):
        old = store.write(
            "很久以前的禁止",
            type="decision",
            source="agent-a",
            lesson_role="avoid",
            created=_days_ago(400),
        )
        plan = store.distill_plan()
        assert plan["candidates"] == []
        assert [row["id"] for row in plan["negative_salience"]] == [old["id"]]
        assert plan["negative_salience"][0]["signals"] == ["avoid"]
        assert plan["negative_salience"][0]["score"] == 1.0

    def test_low_confidence_and_contradiction_count(self, store: MemoryStore):
        low = store.write("把握不大的观察", type="insight", source="agent-a", confidence=0.2)
        disputed = store.write("有争议的结论", type="insight", source="agent-a")
        store.feedback(disputed["id"], "agent-a", outcome="contradiction")
        dangling = store.write(
            "指向不存在的目标",
            type="insight",
            source="agent-a",
            links=["contradicts:missing-target"],
        )
        plan = store.distill_plan()
        by_id = {row["id"]: row for row in plan["negative_salience"]}
        assert by_id[low["id"]]["signals"] == ["low_confidence"]
        assert by_id[low["id"]]["score"] == 0.5
        assert by_id[disputed["id"]]["signals"] == ["contradiction_count"]
        assert by_id[disputed["id"]]["contradiction_count"] == 1
        assert by_id[disputed["id"]]["confidence"] == 0.5
        assert by_id[disputed["id"]]["score"] == 1.0
        assert dangling["id"] not in by_id
        assert plan["contradiction_pairs"] == []

    def test_avoid_alone_does_not_invent_suggested_action(self, store: MemoryStore):
        mem = store.write("不要覆盖生产配置", type="decision", source="agent-a", lesson_role="avoid")
        store.feedback(mem["id"], "agent-a")
        plan = store.distill_plan()
        (cand,) = plan["candidates"]
        assert cand["id"] == mem["id"]
        assert cand["suggested_action"] is None
        assert plan["suggested_actions"] == []
        assert plan["negative_salience"][0]["id"] == mem["id"]
        assert plan["negative_salience"][0]["signals"] == ["avoid"]


class TestLessonRoleAdapters:
    def test_cli_round_trip(self, tmp_path, capsys: pytest.CaptureFixture[str]):
        root = str(tmp_path / "lesson-root")
        assert cli_main(
            ["--root", root, "write", "不要盲重试", "decision", "agent-a", "--lesson-role", "avoid"]
        ) == 0
        written = json.loads(capsys.readouterr().out)
        assert written["lesson_role"] == "avoid"
        assert cli_main(["--root", root, "get", written["id"]]) == 0
        got = json.loads(capsys.readouterr().out)
        assert got["lesson_role"] == "avoid"

    def test_mcp_write_passthrough(self, tmp_path):
        from compound_memory import server

        server.configure(tmp_path / "mcp-root")
        written = server.memory_write(
            content="不要盲重试",
            type="decision",
            source="agent-a",
            lesson_role="avoid",
        )
        assert written["lesson_role"] == "avoid"
        with pytest.raises(ValueError, match="lesson_role"):
            server.memory_write(
                content="坏角色",
                type="insight",
                source="agent-a",
                lesson_role="nope",
            )
