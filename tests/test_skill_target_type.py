"""ADR-0013：高活性 episode 的 promote 行带 target_type=skill，只建议不代写。

门槛仍是既有 promotion_candidate（episode 且 uses >= PROMOTION_USES_THRESHOLD，
并落在 distill 活性窗内）。merge / review 不带 target_type。同 ns 已有活动
skill 同 key 时，报告改判 review，dream-apply 的 type=skill 晋升不调用
distill_apply。dream --propose 与 maintain 不写步骤正文、不 apply。
"""

from __future__ import annotations

import datetime as dt
import json
import re

import pytest

from compound_memory.cli import main as cli_main
from compound_memory.storage import PROMOTION_USES_THRESHOLD, MemoryStore
from compound_memory.storage.distill import TARGET_TYPE_SKILL
from compound_memory.storage.dream import REASON_ACTIVE_SKILL
from conftest import CLOCK_DATE


def _days_ago(n: int) -> str:
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


def _heat(store: MemoryStore, mem_id: str, n: int = PROMOTION_USES_THRESHOLD) -> None:
    for index in range(n):
        store.feedback(mem_id, f"agent-{index}")


def _by_id(plan: dict) -> dict[str, dict]:
    return {row["id"]: row for row in plan["candidates"]}


def _promote_rows(plan: dict) -> list[dict]:
    return [row for row in plan["suggested_actions"] if row["suggested_action"] == "promote"]


def _hot_episode(store: MemoryStore, content: str, key: str | None = None) -> dict:
    mem = store.write(content=content, type="episode", source="agent-a", key=key)
    _heat(store, mem["id"])
    return mem


class TestPromoteTargetType:
    def test_promote_row_carries_target_type_skill(self, store: MemoryStore):
        hot = _hot_episode(store, "反复验证的部署步骤", key="deploy")
        plan = store.distill_plan()
        report = store.dream()

        cand = _by_id(plan)[hot["id"]]
        assert cand["promotion_candidate"] is True
        assert cand["suggested_action"] == "promote"
        assert cand["target_type"] == TARGET_TYPE_SKILL == "skill"
        (promote,) = _promote_rows(plan)
        assert promote["target_type"] == "skill"
        assert promote["source_ids"] == [hot["id"]]
        assert promote["type"] == "episode"
        assert promote["key"] == "deploy"
        assert report["distill"]["candidates"] == plan["candidates"]
        assert report["distill"]["suggested_actions"] == plan["suggested_actions"]
        assert report["dry_run"] is True

    def test_below_threshold_has_no_target_type(self, store: MemoryStore):
        cold = store.write(content="只用过四次", type="episode", source="agent-a", key="deploy")
        _heat(store, cold["id"], n=PROMOTION_USES_THRESHOLD - 1)
        plan = store.distill_plan()
        cand = _by_id(plan)[cold["id"]]
        assert cand["promotion_candidate"] is False
        assert cand["suggested_action"] is None
        assert "target_type" not in cand
        assert _promote_rows(plan) == []

    def test_merge_and_review_rows_omit_target_type(self, store: MemoryStore):
        """同 key 多版仍是 merge，即便旁边有同 key 的 skill，也不改判成 skill。"""
        store.write(content="已有技能步骤", type="skill", source="agent-a", key="deploy")
        older = store.write(
            content="部署记录甲",
            type="episode",
            source="agent-a",
            key="deploy",
            created=_days_ago(2),
        )
        newer = store.write(
            content="部署记录乙",
            type="episode",
            source="agent-a",
            key="deploy",
            created=_days_ago(1),
        )
        _heat(store, older["id"])
        _heat(store, newer["id"])
        plan = store.distill_plan()
        by_id = _by_id(plan)
        for mem_id in (older["id"], newer["id"]):
            assert by_id[mem_id]["promotion_candidate"] is True
            assert by_id[mem_id]["suggested_action"] == "merge"
            assert "target_type" not in by_id[mem_id]
        assert _promote_rows(plan) == []
        merges = [row for row in plan["suggested_actions"] if row["suggested_action"] == "merge"]
        assert merges
        assert all("target_type" not in row for row in merges)
        assert any(row["type"] == "episode" and row["key"] == "deploy" for row in merges)


class TestActiveSkillDedupe:
    def test_same_key_active_skill_demotes_promote_to_review(self, store: MemoryStore):
        store.write(content="已有技能", type="skill", source="agent-a", key="deploy", created=_days_ago(400))
        hot = _hot_episode(store, "又一条高活性经历", key="deploy")
        plan = store.distill_plan()
        cand = _by_id(plan)[hot["id"]]
        assert cand["promotion_candidate"] is True
        assert cand["suggested_action"] == "review"
        assert "target_type" not in cand
        assert _promote_rows(plan) == []
        assert hot["id"] not in {
            cid for row in plan["suggested_actions"] if row["suggested_action"] == "promote" for cid in row["source_ids"]
        }

    def test_episode_without_key_stays_promote(self, store: MemoryStore):
        """报告阶段用 episode 自己的 key。没有 key 就对不上已有 skill。"""
        store.write(content="已有技能", type="skill", source="agent-a", key="deploy")
        hot = _hot_episode(store, "没有 key 的高活性经历")
        plan = store.distill_plan()
        cand = _by_id(plan)[hot["id"]]
        assert cand["key"] is None
        assert cand["suggested_action"] == "promote"
        assert cand["target_type"] == "skill"

    def test_archived_expired_and_bad_date_skills_do_not_block(self, store: MemoryStore):
        archived = store.write(content="已归档技能", type="skill", source="agent-a", key="archived-key")
        found = store.find(archived["id"])
        assert found is not None
        store._archive(found)
        store.write(
            content="已过期技能",
            type="skill",
            source="agent-a",
            key="expired-key",
            valid_until=_days_ago(1),
        )
        bad = store.write(content="坏日期技能", type="skill", source="agent-a", key="bad-key")
        path = store.ns_root / "_shared" / "skill" / f"{bad['id']}.md"
        text, n = re.subn(r"^created:.*$", "created: not-a-date", path.read_text(encoding="utf-8"), count=1, flags=re.M)
        assert n == 1
        path.write_text(text, encoding="utf-8")

        for key in ("archived-key", "expired-key", "bad-key"):
            hot = _hot_episode(store, f"经历 {key}", key=key)
            cand = _by_id(store.distill_plan())[hot["id"]]
            assert cand["suggested_action"] == "promote", key
            assert cand["target_type"] == "skill", key

    def test_skill_in_another_namespace_does_not_block(self, store: MemoryStore):
        store.write(
            content="私有技能",
            type="skill",
            source="agent-zcode",
            ns="agent-zcode",
            key="deploy",
        )
        hot = _hot_episode(store, "共享区经历", key="deploy")
        cand = _by_id(store.distill_plan())[hot["id"]]
        assert cand["suggested_action"] == "promote"
        assert cand["target_type"] == "skill"


class TestDreamApplySkill:
    def _skill_action(self, source_id: str, key: str, content: str = "调用方写的步骤") -> dict:
        return {
            "content": content,
            "type": "skill",
            "source_ids": [source_id],
            "key": key,
            "promote": True,
        }

    def test_caller_supplies_skill_body_and_promote_opt_in(self, store: MemoryStore):
        hot = _hot_episode(store, "源经历正文不该被抄成技能")
        refused = store.dream_apply(
            [{"content": "调用方写的步骤", "type": "skill", "source_ids": [hot["id"]], "key": "deploy"}],
            source="agent-a",
        )
        assert refused["applied"] == []
        assert refused["skipped"][0]["reason"] == "promotion_candidate requires explicit opt-in"
        assert store.get(hot["id"])["archived"] is False

        report = store.dream_apply([self._skill_action(hot["id"], "deploy")], source="agent-a")
        applied = report["applied"][0]
        assert applied["signal"] == "promotion_candidate"
        product = applied["result"]
        assert product["found"] is True
        assert product["type"] == "skill"
        assert product["key"] == "deploy"
        assert product["content"] == "调用方写的步骤"
        assert product["content"] != "源经历正文不该被抄成技能"
        assert product["origin"] == "distillation"
        assert product["lesson_role"] is None
        body = (store.ns_root / "_shared" / "skill" / f"{product['id']}.md").read_text(encoding="utf-8")
        assert "lesson_role" not in body
        assert "调用方写的步骤" in body
        assert store.get(hot["id"])["archived"] is True

    def test_existing_skill_skips_promotion_without_distill_apply(
        self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
    ):
        store.write(content="已有技能", type="skill", source="agent-a", key="deploy")
        hot = _hot_episode(store, "还想再晋升一条", key="deploy")

        def boom(*_args: object, **_kwargs: object) -> dict:
            raise AssertionError("distill_apply must not run")

        monkeypatch.setattr(store, "distill_apply", boom)
        report = store.dream_apply([self._skill_action(hot["id"], "deploy")], source="agent-a")
        assert report["applied"] == []
        assert report["planned"] == []
        assert report["skipped"][0]["reason"] == REASON_ACTIVE_SKILL
        assert store.get(hot["id"])["archived"] is False

        dry = store.dream_apply(
            [self._skill_action(hot["id"], "deploy")],
            source="agent-a",
            dry_run=True,
        )
        assert dry["planned"] == []
        assert dry["applied"] == []
        assert dry["skipped"][0]["reason"] == REASON_ACTIVE_SKILL

    def test_apply_uses_submitted_key_not_episode_key(self, store: MemoryStore):
        """报告阶段对不上（episode 无 key），apply 用调用方提交的 key。"""
        store.write(content="已有技能", type="skill", source="agent-a", key="deploy")
        hot = _hot_episode(store, "无 key 经历")
        blocked = store.dream_apply([self._skill_action(hot["id"], "deploy")], source="agent-a")
        assert blocked["applied"] == []
        assert blocked["skipped"][0]["reason"] == REASON_ACTIVE_SKILL
        assert store.get(hot["id"])["archived"] is False

        fresh = store.dream_apply(
            [self._skill_action(hot["id"], "fresh-skill", content="另一套步骤")],
            source="agent-a",
        )
        assert fresh["applied"][0]["result"]["key"] == "fresh-skill"
        assert fresh["applied"][0]["result"]["content"] == "另一套步骤"

    def test_insight_promotion_still_applies_when_skill_exists(self, store: MemoryStore):
        store.write(content="已有技能", type="skill", source="agent-a", key="deploy")
        hot = _hot_episode(store, "可以蒸馏成洞察", key="deploy")
        report = store.dream_apply(
            [{
                "content": "调用方写的洞察",
                "type": "insight",
                "source_ids": [hot["id"]],
                "key": "deploy",
                "promote": True,
            }],
            source="agent-a",
        )
        assert report["skipped"] == []
        assert report["applied"][0]["signal"] == "promotion_candidate"
        assert report["applied"][0]["result"]["type"] == "insight"

    def test_same_type_skill_merge_still_applies(self, store: MemoryStore):
        first = store.write(content="技能甲", type="skill", source="agent-a", key="deploy")
        second = store.write(content="技能乙", type="skill", source="agent-b", key="deploy")
        report = store.dream_apply(
            [{
                "content": "调用方合并后的步骤",
                "type": "skill",
                "source_ids": [first["id"], second["id"]],
                "key": "deploy",
            }],
            source="agent-a",
        )
        assert report["skipped"] == []
        applied = report["applied"][0]
        assert applied["signal"] == "key_duplicates"
        assert applied["result"]["found"] is True
        assert applied["result"]["type"] == "skill"
        assert applied["result"]["content"] == "调用方合并后的步骤"
        assert applied["result"]["origin"] == "distillation"

    def test_second_skill_promotion_in_one_package_is_skipped(
        self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
    ):
        first = _hot_episode(store, "经历甲")
        second = _hot_episode(store, "经历乙")
        calls: list[str] = []
        real = store.distill_apply

        def spy(*args: object, **kwargs: object) -> dict:
            calls.append("distill_apply")
            return real(*args, **kwargs)

        monkeypatch.setattr(store, "distill_apply", spy)
        report = store.dream_apply(
            [
                self._skill_action(first["id"], "shared-skill", content="第一条步骤"),
                self._skill_action(second["id"], "shared-skill", content="第二条步骤"),
            ],
            source="agent-a",
        )
        assert calls == ["distill_apply"]
        assert len(report["applied"]) == 1
        assert report["applied"][0]["result"]["content"] == "第一条步骤"
        assert report["skipped"][0]["reason"] == REASON_ACTIVE_SKILL
        assert report["skipped"][0]["source_ids"] == [second["id"]]

    def test_dry_run_second_skill_promotion_is_skipped(
        self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
    ):
        """dry-run 不落盘，仍按真 apply 的顺序占住 key，第二条进 skipped。"""
        first = _hot_episode(store, "经历甲")
        second = _hot_episode(store, "经历乙")

        def boom(*_args: object, **_kwargs: object) -> dict:
            raise AssertionError("dry-run must not call distill_apply")

        monkeypatch.setattr(store, "distill_apply", boom)
        report = store.dream_apply(
            [
                self._skill_action(first["id"], "shared-skill", content="第一条步骤"),
                self._skill_action(second["id"], "shared-skill", content="第二条步骤"),
            ],
            source="agent-a",
            dry_run=True,
        )
        assert report["applied"] == []
        assert [row["source_ids"] for row in report["planned"]] == [[first["id"]]]
        assert report["skipped"][0]["reason"] == REASON_ACTIVE_SKILL
        assert report["skipped"][0]["source_ids"] == [second["id"]]
        assert store.get(first["id"])["archived"] is False
        assert store.get(second["id"])["archived"] is False

    def test_empty_skill_content_rejects_the_whole_list(
        self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch
    ):
        hot = _hot_episode(store, "经历")
        older = store.write(content="事实甲", type="fact", source="agent-a", key="proj", created=_days_ago(2))
        newer = store.write(content="事实乙", type="fact", source="agent-a", key="proj", created=_days_ago(1))

        def boom(*_args: object, **_kwargs: object) -> dict:
            raise AssertionError("distill_apply must not run")

        monkeypatch.setattr(store, "distill_apply", boom)
        with pytest.raises(ValueError, match="non-empty content"):
            store.dream_apply(
                [
                    {"content": "   ", "type": "skill", "source_ids": [hot["id"]], "key": "deploy", "promote": True},
                    {
                        "content": "合并后的事实",
                        "type": "fact",
                        "source_ids": [older["id"], newer["id"]],
                        "key": "proj",
                    },
                ],
                source="agent-a",
            )
        assert store.get(hot["id"])["archived"] is False
        assert store.get(older["id"])["archived"] is False


class TestProposeAndMaintainStayClear:
    def test_dream_propose_does_not_write_skill_prose(
        self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        from compound_memory import cli as cli_mod

        prose = "UNIQUE_EPISODE_PROSE_do_not_copy"
        _hot_episode(store, prose, key="deploy")
        assert store.dream_proposals() == []
        monkeypatch.setattr(cli_mod, "auto_encoder", lambda: None)
        real_open = cli_mod._open_store

        def _pinned(args: object) -> MemoryStore:
            opened = real_open(args)  # type: ignore[arg-type]
            opened._clock = lambda: CLOCK_DATE
            return opened

        monkeypatch.setattr(cli_mod, "_open_store", _pinned)
        assert cli_main(["--root", str(store.root), "dream", "--propose"]) == 0
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        path = store.root / "distill" / "dream-proposals.json"
        raw = path.read_text(encoding="utf-8")
        assert raw == "[]\n"
        assert prose not in raw
        assert "skill" not in raw
        cand = _by_id(report["distill"])
        assert any(row.get("target_type") == "skill" for row in cand.values())
        assert report["dry_run"] is True

    def test_maintain_does_not_propose_or_apply(
        self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        from compound_memory import cli as cli_mod

        hot = _hot_episode(store, "维护时不该落成技能", key="deploy")
        monkeypatch.setattr(cli_mod, "auto_encoder", lambda: None)
        assert cli_main(
            ["--root", str(store.root), "maintain", "--now", CLOCK_DATE.isoformat()]
        ) == 0
        capsys.readouterr()
        assert not (store.root / "distill" / "dream-proposals.json").exists()
        plan = json.loads((store.root / "distill" / "last-plan.json").read_text(encoding="utf-8"))
        assert plan["dry_run"] is True
        assert _by_id(plan["distill"])[hot["id"]]["target_type"] == "skill"
        assert store.get(hot["id"])["archived"] is False


def test_lesson_role_stays_empty_on_plain_skill_write(store: MemoryStore):
    mem = store.write(content="普通技能", type="skill", source="agent-a", key="plain-skill")
    assert mem["lesson_role"] is None
    text = (store.ns_root / "_shared" / "skill" / f"{mem['id']}.md").read_text(encoding="utf-8")
    assert "lesson_role" not in text
    assert store.get(mem["id"])["lesson_role"] is None


def test_server_module_does_not_grow_a_sixth_tool():
    """登记面以 tests/contracts/test_mcp_surface.py 为准。这里只钉模块没有新的 tool 函数。"""
    import compound_memory.server as server

    tool_fns = [
        name
        for name in ("memory_write", "memory_search", "memory_get", "memory_link", "memory_feedback")
        if callable(getattr(server, name, None))
    ]
    assert tool_fns == [
        "memory_write",
        "memory_search",
        "memory_get",
        "memory_link",
        "memory_feedback",
    ]
    assert not hasattr(server, "memory_maintain")
