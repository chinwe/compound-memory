"""建议动作标注（#75）：distill_plan / dream 报告上的 suggested_action。

词汇只有 merge / promote / review。同 key 多版（含活性门外的 key_duplicates）
自动列成 merge 簇，成员用 source_ids 标识；高活性 episode 标 promote；
possible_dup_of（含跨 key 弱相似）标 review，且 dream-apply 仍不收。
不新增写路径、不写 supersedes frontmatter。
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

from compound_memory.cli import build_parser
from compound_memory.storage import MemoryStore
from compound_memory.storage.distill import SUGGESTED_ACTIONS
from conftest import CLOCK_DATE


def _days_ago(n: int) -> str:
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


def _commit_count(store: MemoryStore) -> int:
    proc = subprocess.run(
        ["git", "-C", str(store.root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(proc.stdout.strip())


_ACTION_KEYS = {"suggested_action", "source_ids", "signal", "type", "key"}


def _actions(plan: dict, name: str) -> list[dict]:
    return [row for row in plan["suggested_actions"] if row["suggested_action"] == name]


class TestVocabulary:
    def test_v1_actions_are_merge_promote_review(self):
        assert SUGGESTED_ACTIONS == ("merge", "promote", "review")

    def test_cli_has_no_distill_suggest_command(self):
        action = next(
            item for item in build_parser()._actions if item.__class__.__name__ == "_SubParsersAction"
        )
        assert "distill-suggest" not in action.choices
        text = action.choices["distill-plan"].format_help()
        assert "suggested_action" in text
        assert "merge" in text and "promote" in text and "review" in text


class TestMergeGroup:
    def test_same_key_over_threshold_group_is_merge_cluster(self, store: MemoryStore):
        """同 key 且过活性门：merge 簇用 source_ids，不另叫 synthesize。"""
        older = store.write(
            content="deploy 流程记录甲",
            type="episode",
            source="agent-a",
            key="deploy",
            created=_days_ago(2),
        )
        newer = store.write(
            content="deploy 流程记录乙",
            type="episode",
            source="agent-a",
            key="deploy",
            created=_days_ago(1),
        )
        store.feedback(older["id"], "agent-a")
        store.feedback(newer["id"], "agent-a")
        before = _commit_count(store)

        plan = store.distill_plan()
        report = store.dream()

        assert _commit_count(store) == before
        assert report["dry_run"] is True
        assert report["distill"]["suggested_actions"] == plan["suggested_actions"]
        by_id = {row["id"]: row for row in plan["candidates"]}
        assert by_id[older["id"]]["suggested_action"] == "merge"
        assert by_id[newer["id"]]["suggested_action"] == "merge"
        assert by_id[older["id"]]["merge_with"] == [newer["id"]]
        (group,) = plan["key_duplicates"]
        assert group["suggested_action"] == "merge"
        assert group["source_ids"] == [older["id"], newer["id"]]
        merges = _actions(plan, "merge")
        assert len(merges) == 1
        assert set(merges[0]) == _ACTION_KEYS
        assert merges[0]["source_ids"] == [older["id"], newer["id"]]
        assert merges[0]["signal"] == "merge_with"
        assert merges[0]["type"] == "episode"
        assert merges[0]["key"] == "deploy"
        blob = json.dumps(plan)
        assert "synthesize" not in blob
        assert "supersede" not in blob

    def test_below_activity_gate_same_key_group_still_auto_listed(self, store: MemoryStore):
        """uses=0 的同 key 多版进不了主候选，仍作为 merge 簇自动列出（要正文才落库）。"""
        stale = store.write(
            content="proj-x 的旧版结论",
            type="fact",
            source="agent-a",
            key="proj-x",
            created=_days_ago(3),
        )
        fresh = store.write(
            content="proj-x 的新版结论",
            type="fact",
            source="agent-a",
            key="proj-x",
            created=_days_ago(1),
        )
        plan = store.distill_plan()
        assert plan["candidates"] == []
        (group,) = plan["key_duplicates"]
        assert group["suggested_action"] == "merge"
        assert group["source_ids"] == [stale["id"], fresh["id"]]
        (merge,) = _actions(plan, "merge")
        assert merge["signal"] == "key_duplicates"
        assert merge["source_ids"] == [stale["id"], fresh["id"]]
        assert merge["key"] == "proj-x"
        assert _actions(plan, "promote") == []
        assert _actions(plan, "review") == []


class TestPromoteExample:
    def test_hot_episode_is_promote_and_apply_still_needs_opt_in(self, store: MemoryStore):
        hot = store.write(content="反复验证的紫丁香部署经验", type="episode", source="agent-a")
        for agent in ("a", "b", "c", "d", "e"):
            store.feedback(hot["id"], agent)
        plan = store.distill_plan()
        (cand,) = plan["candidates"]
        assert cand["id"] == hot["id"]
        assert cand["promotion_candidate"] is True
        assert cand["suggested_action"] == "promote"
        # 【契约变更 ADR-0013】无同 key 活动 skill 时，promote 行带 target_type。
        # 本条 episode 没有 key，报告阶段对不上任何 skill。
        assert cand["target_type"] == "skill"
        (promote,) = _actions(plan, "promote")
        assert set(promote) == _ACTION_KEYS | {"target_type"}
        assert promote["target_type"] == "skill"
        assert promote["source_ids"] == [hot["id"]]
        assert promote["signal"] == "promotion_candidate"
        assert promote["type"] == "episode"
        assert promote["key"] is None
        assert _actions(plan, "merge") == []

        refused = store.dream_apply(
            [{"content": "晋升后的洞察", "type": "insight", "source_ids": [hot["id"]]}],
            source="agent-a",
        )
        assert refused["applied"] == []
        assert refused["skipped"][0]["reason"] == "promotion_candidate requires explicit opt-in"

        admitted = store.dream_apply(
            [{"content": "晋升后的洞察", "type": "insight", "source_ids": [hot["id"]], "promote": True}],
            source="agent-a",
            dry_run=True,
        )
        assert admitted["planned"][0]["signal"] == "promotion_candidate"
        assert admitted["applied"] == []


class TestPossibleDupReview:
    def test_cross_key_possible_dup_is_review_and_not_applied(self, store: MemoryStore):
        """跨 key 弱相似只标 review。dream-apply 不放行；综合它们仍走既有 distill_apply。"""
        dup_a = store.write(
            content="compound-memory 蒸馏管线把候选清单交给 agent 判断",
            type="episode",
            source="agent-a",
            key="pipeline-a",
        )
        dup_b = store.write(
            content="compound-memory 蒸馏管线把候选清单交给调用方判断",
            type="episode",
            source="agent-a",
            key="pipeline-b",
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
        plan = store.distill_plan(min_uses=0)
        by_id = {row["id"]: row for row in plan["candidates"]}
        assert by_id[dup_a["id"]]["suggested_action"] == "review"
        assert by_id[dup_b["id"]]["suggested_action"] == "review"
        assert dup_b["id"] in by_id[dup_a["id"]]["possible_dup_of"]
        reviews = _actions(plan, "review")
        assert len(reviews) == 1
        assert set(reviews[0]) == _ACTION_KEYS
        assert reviews[0]["signal"] == "possible_dup_of"
        assert reviews[0]["source_ids"] == sorted([dup_a["id"], dup_b["id"]])
        assert reviews[0]["key"] is None
        assert reviews[0]["type"] == "episode"
        assert _actions(plan, "merge") == []
        blob = json.dumps(plan)
        assert "synthesize" not in blob

        skipped = store.dream_apply(
            [{"content": "不该自动合并的弱相似", "type": "insight", "source_ids": [dup_a["id"], dup_b["id"]]}],
            source="agent-a",
            min_uses=0,
        )
        assert skipped["applied"] == []
        assert skipped["skipped"][0]["reason"] == "possible_dup_of is report-only"
        assert store.get(dup_a["id"])["archived"] is False

    def test_same_key_similar_pair_stays_merge_not_review(self, store: MemoryStore):
        """同 key 强信号压过弱相似与晋升：主标注是 merge，不再另开一条 review 簇。"""
        older = store.write(
            content="compound-memory 蒸馏管线把候选清单交给 agent 判断",
            type="episode",
            source="agent-a",
            key="pipeline",
            created=_days_ago(2),
        )
        newer = store.write(
            content="compound-memory 蒸馏管线把候选清单交给调用方判断",
            type="episode",
            source="agent-a",
            key="pipeline",
            created=_days_ago(1),
        )
        for mem_id in (older["id"], newer["id"]):
            for agent in ("a", "b", "c", "d", "e"):
                store.feedback(mem_id, agent)
        for title in (
            "Python GIL 全局解释器锁行为",
            "Go goroutine 并发模型要点",
            "Redis 持久化 RDB 与 AOF 差异",
            "Kubernetes Pod 亲和性配置",
            "HTTP ETag 协商缓存机制",
            "Vercel Serverless 函数超时",
        ):
            store.write(content=title, type="episode", source="agent-a")
        plan = store.distill_plan(min_uses=0)
        by_id = {row["id"]: row for row in plan["candidates"]}
        assert by_id[older["id"]]["promotion_candidate"] is True
        assert by_id[older["id"]]["possible_dup_of"]
        assert by_id[older["id"]]["suggested_action"] == "merge"
        assert by_id[newer["id"]]["suggested_action"] == "merge"
        assert _actions(plan, "review") == []
        (merge,) = _actions(plan, "merge")
        assert merge["source_ids"] == [older["id"], newer["id"]]
        assert _actions(plan, "promote") == []


class TestNoSignal:
    def test_lone_used_memory_has_null_action(self, store: MemoryStore):
        used = store.write(content="只被用过一次的独有经历", type="episode", source="agent-a")
        store.feedback(used["id"], "agent-a")
        plan = store.distill_plan()
        (cand,) = plan["candidates"]
        assert cand["suggested_action"] is None
        assert plan["suggested_actions"] == []
        assert plan["key_duplicates"] == []


class TestClusterOrder:
    def test_merge_clusters_follow_type_then_key(self, store: MemoryStore):
        store.write(content="b 事实", type="fact", source="agent-a", key="b-key", created=_days_ago(2))
        store.write(content="b 事实新", type="fact", source="agent-a", key="b-key", created=_days_ago(1))
        store.write(content="a 经历", type="episode", source="agent-a", key="a-key", created=_days_ago(2))
        store.write(content="a 经历新", type="episode", source="agent-a", key="a-key", created=_days_ago(1))
        plan = store.distill_plan(min_uses=0)
        assert [(row["type"], row["key"]) for row in _actions(plan, "merge")] == [
            ("episode", "a-key"),
            ("fact", "b-key"),
        ]
