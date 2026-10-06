"""feedback 动词契约（store 层五要素 characterization）。

契约出处：#26 P1（置信度精确公式）/ P2（side effects 含归档自动复活）；
#31（feedback 每次一 commit，消息模板）。复利闭环是一等公民——公式漂移在此必红。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE
from anchors import FOREIGN, OWNER, PRIVATE_NS, commit_count, days_ago, last_message, matches


def _shared_mem(store: MemoryStore, source: str = "agent-a") -> dict:
    return store.write("contract feedback target", type="fact", source=source, key="fb")


class TestConfidenceFormula:
    """P1：conf bump = 0.1/次 + 0.15 仅当「新验证者 ∧ ≠source」；
    validated_by 去重（同 agent 重复反馈不再加验证分）；round 3 位。"""

    def test_source_self_feedback_bumps_point_one(self, store: MemoryStore):
        """source 自己首次反馈：只加使用分 +0.1（agent == source 不算新验证者）。"""
        mem = _shared_mem(store, source="agent-a")
        out = store.feedback(mem["id"], "agent-a")
        assert out["uses"] == 1
        assert out["confidence"] == 0.6
        assert out["validated_by"] == ["agent-a"]

    def test_new_validator_cross_agent_bumps_point_two_five(self, store: MemoryStore):
        """不同 agent 首次验证：+0.1 + 0.15 = +0.25。"""
        mem = _shared_mem(store, source="agent-a")
        out = store.feedback(mem["id"], "agent-b")
        assert out["confidence"] == 0.75
        assert out["validated_by"] == ["agent-b"]

    def test_repeat_validator_gets_use_bump_only(self, store: MemoryStore):
        """同 agent 重复反馈：validated_by 去重后只 +0.1——验证分不重复发放。"""
        mem = _shared_mem(store, source="agent-a")
        store.feedback(mem["id"], "agent-b")  # 0.75
        out = store.feedback(mem["id"], "agent-b")  # 去重：只 +0.1
        assert out["confidence"] == 0.85
        assert out["validated_by"] == ["agent-b"]

    def test_source_later_joins_as_validator_without_cross_bump(self, store: MemoryStore):
        """source 首次出现在 validated_by：agent == source，只加使用分。"""
        mem = _shared_mem(store, source="agent-a")
        store.feedback(mem["id"], "agent-b")  # 0.75, validated_by=[b]
        out = store.feedback(mem["id"], "agent-a")  # a != b 是新验证者但 == source：+0.1
        assert out["confidence"] == 0.85
        assert out["validated_by"] == ["agent-b", "agent-a"]

    def test_confidence_capped_at_one(self, store: MemoryStore):
        mem = _shared_mem(store, source="agent-a")
        for _ in range(10):
            store.feedback(mem["id"], "agent-a")
        assert store.get(mem["id"])["confidence"] == 1.0

    def test_confidence_rounded_to_three_decimals(self, store: MemoryStore):
        """round 3 位可观测：0.5+0.1+0.1 的浮点尾差（0.7999…）被修到 0.8。"""
        mem = _shared_mem(store, source="agent-a")
        store.feedback(mem["id"], "agent-a")
        store.feedback(mem["id"], "agent-a")
        out = store.feedback(mem["id"], "agent-a")
        assert out["confidence"] == 0.8


class TestOutcomeFolding:
    """ADR-0007 折算表：failure −0.2 重复累计、地板 0.05、unknown 仅记账、未知值 ValueError。
    success 缺省路径的单调段由 TestConfidenceFormula 继续钉住（老调用方零破坏）。"""

    def test_failure_lowers_confidence_and_repeats_accumulate(self, store: MemoryStore):
        """负面不对称：failure −0.2（0.5→0.3→0.1→0.05），重复 failure 累计折算。"""
        mem = _shared_mem(store)
        assert store.feedback(mem["id"], "agent-b", outcome="failure")["confidence"] == 0.3
        assert store.feedback(mem["id"], "agent-b", outcome="failure")["confidence"] == 0.1
        assert store.feedback(mem["id"], "agent-b", outcome="failure")["confidence"] == 0.05

    def test_floor_keeps_memory_retrievable_and_active(self, store: MemoryStore):
        """地板 0.05 的意义：折到地板的记忆仍可检索、未归档——留待后续证据翻身。"""
        mem = _shared_mem(store)
        for _ in range(5):
            store.feedback(mem["id"], "agent-b", outcome="failure")
        assert store.get(mem["id"])["confidence"] == 0.05
        assert store.get(mem["id"])["archived"] is False
        assert [h["id"] for h in store.search("contract feedback target", top_k=10)] == [mem["id"]]

    def test_success_after_failure_recovers(self, store: MemoryStore):
        """可升可降：failure 0.3 后 source 自己 success +0.1 → 0.4（单调公式已废）。"""
        mem = _shared_mem(store)
        store.feedback(mem["id"], "agent-b", outcome="failure")
        out = store.feedback(mem["id"], "agent-a", outcome="success")
        assert out["confidence"] == 0.4

    def test_cross_host_bonus_is_success_only(self, store: MemoryStore):
        """+0.15 跨宿主首验只属于 success：failure 是负证据，不吃验证加分、不入 validated_by。"""
        mem = _shared_mem(store)
        out = store.feedback(mem["id"], "agent-b", outcome="failure")
        assert out["confidence"] == 0.3  # 0.5 − 0.2（无 +0.15 混算）
        assert out["validated_by"] == []

    def test_unknown_records_event_without_moving_numbers(self, store: MemoryStore):
        """unknown 仅记事件：uses+1、last_used、明细入块，conf/validated_by 原地。"""
        mem = _shared_mem(store)
        out = store.feedback(mem["id"], "agent-b", outcome="unknown")
        assert out["confidence"] == 0.5
        assert out["uses"] == 1
        assert out["last_used"] == CLOCK_DATE.isoformat()
        assert out["validated_by"] == []
        m = matches("feedback", last_message(store))
        assert m["outcome"] == "unknown"

    def test_unknown_outcome_value_is_caller_error(self, store: MemoryStore):
        """未知 outcome 是调用方错误：ValueError、零提交、记忆原样（原子）。"""
        mem = _shared_mem(store)
        before = commit_count(store)
        with pytest.raises(ValueError, match="outcome"):
            store.feedback(mem["id"], "agent-b", outcome="bogus")
        assert commit_count(store) == before
        got = store.get(mem["id"])
        assert got["uses"] == 0 and got["confidence"] == 0.5

    def test_commit_message_carries_outcome(self, store: MemoryStore):
        """提交模板扩展（ADR-0007）：outcome= 段先行，uses/conf 后随。"""
        mem = _shared_mem(store, source="agent-a")
        store.feedback(mem["id"], "agent-b", outcome="failure")
        m = matches("feedback", last_message(store))
        assert m["outcome"] == "failure"
        assert m["uses"] == "1"
        assert m["conf"] == "0.3"


class TestEvidenceBlock:
    """ADR-0007 混合三层①：frontmatter 证据块是运行时唯一数据源——惰性迁移、
    首次 feedback 落盘、recent 明细 cap 10。"""

    def test_new_memory_has_no_block_until_first_feedback(self, store: MemoryStore):
        mem = _shared_mem(store)
        raw = (store.ns_root / "_shared" / "fact" / f"{mem['id']}.md").read_text()
        assert "evidence" not in raw  # 普通写不落证据块（存量兼容面）
        assert store.get(mem["id"])["evidence"] is None

    def test_first_feedback_materializes_block(self, store: MemoryStore):
        mem = _shared_mem(store)
        store.feedback(mem["id"], "agent-b")
        ev = store.get(mem["id"])["evidence"]
        assert ev["success_count"] == 1
        assert ev["failure_count"] == 0 and ev["contradiction_count"] == 0
        assert ev["last_verified"] == CLOCK_DATE.isoformat()
        assert ev["recent"] == [{"date": CLOCK_DATE.isoformat(), "agent": "agent-b", "outcome": "success"}]

    def test_legacy_uses_map_to_success_count(self, store: MemoryStore):
        """惰性迁移：无块旧记忆读为 {success_count: uses, 0, 0}——uses 无差别映射
        success（诚实反映「只知被用过」），首次 feedback 落盘写块。"""
        mem = _shared_mem(store)
        legacy = store.find(mem["id"])
        assert legacy is not None
        legacy.uses = 3  # 模拟无证据块的存量记忆
        store._save(legacy)
        out = store.feedback(mem["id"], "agent-b")
        assert out["uses"] == 4
        assert out["evidence"]["success_count"] == 4  # 3 存量 + 1 本次

    def test_recent_details_capped_at_ten(self, store: MemoryStore):
        """明细 cap 10（frontmatter parse 是延迟大头，perf 守门）：老明细语义化进计数不丢失。"""
        mem = _shared_mem(store)
        for _ in range(12):
            store.feedback(mem["id"], "agent-b")
        ev = store.get(mem["id"])["evidence"]
        assert ev["success_count"] == 12
        assert len(ev["recent"]) == 10


class TestContradictionOutcome:
    """contradiction：数值冻结 + 登记队列独立行型；裁决经 review-resolve 二选一
    （维持 ⇒ 解冻并折算 failure −0.2；确错 ⇒ 沿用归档语义）。"""

    def test_contradiction_freezes_number_and_enqueues_row(self, store: MemoryStore):
        mem = _shared_mem(store)
        out = store.feedback(mem["id"], "agent-b", outcome="contradiction")
        assert out["confidence"] == 0.5  # 冻结：数值原地
        assert out["evidence"]["contradiction_count"] == 1
        lines = store.review_queue()
        assert len(lines) == 1
        assert mem["id"] in lines[0] and "agent-b" in lines[0] and "contradiction" in lines[0]

    def test_feedback_during_freeze_moves_nothing(self, store: MemoryStore):
        """冻结期不升不降：事件照记（uses/计数/validated_by），数值原地。"""
        mem = _shared_mem(store)
        store.feedback(mem["id"], "agent-b", outcome="contradiction")
        out = store.feedback(mem["id"], "agent-c")
        assert out["confidence"] == 0.5
        assert out["evidence"]["success_count"] == 1
        assert out["validated_by"] == ["agent-c"]  # contradiction 不入 validated_by

    def test_uphold_unfreezes_and_folds_as_failure(self, store: MemoryStore):
        """裁决「维持有效」：解冻 + 该次 contradiction 折算 failure −0.2。"""
        mem = _shared_mem(store)
        store.feedback(mem["id"], "agent-b", outcome="contradiction")
        out = store.review_resolve([mem["id"]], uphold=True)
        assert out["upheld"] == [mem["id"]]
        got = store.get(mem["id"])
        assert got["archived"] is False
        assert got["confidence"] == 0.3  # 0.5 − 0.2
        assert got["evidence"]["failure_count"] == 1
        assert store.review_queue() == []
        # 解冻后数值恢复联动（source 自己 success 只 +0.1）
        assert store.feedback(mem["id"], "agent-a", outcome="success")["confidence"] == 0.4

    def test_confirm_wrong_archives_via_existing_semantics(self, store: MemoryStore):
        """裁决「确错」：沿用 review_resolve 归档语义（传入 id = 废置方），行清掉。"""
        mem = _shared_mem(store)
        store.feedback(mem["id"], "agent-b", outcome="contradiction")
        store.review_resolve([mem["id"]])
        assert store.get(mem["id"])["archived"] is True
        assert store.review_queue() == []

    def test_uphold_rejected_on_conflict_rows_atomically(self, store: MemoryStore):
        """uphold 只适用 contradiction 行：点名 conflict 行 ValueError 原子拒绝。"""
        store.write("contract uphold old", type="fact", source="agent-a", key="uphold-k")
        new = store.write("contract uphold new", type="fact", source="agent-a", key="uphold-k")
        before = commit_count(store)
        with pytest.raises(ValueError, match="contradiction"):
            store.review_resolve([new["id"]], uphold=True)
        assert len(store.review_queue()) == 1
        assert commit_count(store) == before

    def test_all_clears_contradiction_row_without_fold(self, store: MemoryStore):
        """--all 只清行（无裁决信息）：解冻但不折算——不自动推断裁决方向。"""
        mem = _shared_mem(store)
        store.feedback(mem["id"], "agent-b", outcome="contradiction")
        store.review_resolve(all=True)
        assert store.review_queue() == []
        assert store.get(mem["id"])["confidence"] == 0.5
        # 解冻后数值恢复联动（source 自己 success 只 +0.1）
        assert store.feedback(mem["id"], "agent-a", outcome="success")["confidence"] == 0.6


class TestObsoleteOutcome:
    """obsolete：无条件立即归档（无「obsolete 但未归档」中间态）；复活走
    feedback 自动复活既有通道；数值不是 obsolete 的动作对象。"""

    def test_obsolete_archives_immediately(self, store: MemoryStore):
        mem = _shared_mem(store)
        out = store.feedback(mem["id"], "agent-b", outcome="obsolete")
        assert out["archived"] is True
        assert out["confidence"] == 0.5  # 归档不是数值动作
        assert store.search("contract feedback target", top_k=10) == []  # 归档必不在索引
        assert (store.archive_root / "_shared" / "fact" / f"{mem['id']}.md").exists()
        m = matches("feedback", last_message(store))
        assert m["outcome"] == "obsolete"

    def test_obsolete_revival_rides_feedback_channel(self, store: MemoryStore):
        mem = _shared_mem(store)
        store.feedback(mem["id"], "agent-b", outcome="obsolete")
        out = store.feedback(mem["id"], "agent-b")
        assert out["archived"] is False
        assert [h["id"] for h in store.search("contract feedback target", top_k=10)] == [mem["id"]]

    def test_obsolete_on_archived_memory_stays_archived(self, store: MemoryStore):
        """对已归档记忆报 obsolete：不复活再归档空转，原位记账。"""
        mem = store.write("contract obsolete archived", type="episode", source="agent-a", created=days_ago(120))
        store.decay_sweep()
        out = store.feedback(mem["id"], "agent-b", outcome="obsolete")
        assert out["archived"] is True
        assert out["uses"] == 1


class TestSideEffects:
    """P2：uses+1、last_used=today、归档记忆自动复活（活动区 + 索引同步）、单次 commit。"""

    def test_uses_and_last_used_updated(self, store: MemoryStore):
        mem = _shared_mem(store)
        out = store.feedback(mem["id"], "agent-b")
        assert out["uses"] == 1
        assert out["last_used"] == CLOCK_DATE.isoformat()

    def test_commits_with_template_message(self, store: MemoryStore):
        mem = _shared_mem(store, source="agent-a")
        before = commit_count(store)
        store.feedback(mem["id"], "agent-b")
        assert commit_count(store) == before + 1  # #31：feedback 维持每次一 commit
        m = matches("feedback", last_message(store))
        assert m["id"] == mem["id"]
        assert m["agent"] == "agent-b"
        assert m["outcome"] == "success"  # ADR-0007：缺省 outcome 显式进消息
        assert m["uses"] == "1"
        assert m["conf"] == "0.75"

    def test_archived_memory_auto_revives(self, store: MemoryStore):
        """P2：归档记忆被 feedback 自动复活——文件回活动区、索引同步（重新可检索）、
        且只产生一条 feedback 消息的提交（不是 revive 消息）。"""
        mem = store.write("contract feedback revive", type="episode", source="agent-a", created=days_ago(120))
        store.decay_sweep()
        assert store.search("feedback revive") == []  # 归档必不在索引

        before = commit_count(store)
        out = store.feedback(mem["id"], "agent-b")
        assert out["archived"] is False
        assert out["uses"] == 1
        assert commit_count(store) == before + 1
        matches("feedback", last_message(store))
        assert (store.ns_root / "_shared" / "episode" / f"{mem['id']}.md").exists()
        assert [h["id"] for h in store.search("feedback revive")] == [mem["id"]]  # 复活回到索引

    def test_missing_id_returns_found_false_no_commit(self, store: MemoryStore):
        before = commit_count(store)
        assert store.feedback("nope", "agent-b") == {"found": False}
        assert commit_count(store) == before


class TestPermissionMatrix:
    def test_shared_feedback_any_agent(self, store: MemoryStore):
        mem = _shared_mem(store, source=FOREIGN)
        assert store.feedback(mem["id"], "agent-b")["found"] is True

    def test_private_feedback_denied_atomically(self, store: MemoryStore):
        """外来 agent 对私有记忆的反馈：PermissionError 且原子（uses/confidence 不动）。"""
        priv = store.write("contract private feedback", type="fact", source=OWNER, ns=PRIVATE_NS)
        with pytest.raises(PermissionError):
            store.feedback(priv["id"], FOREIGN)
        got = store.get(priv["id"], reader=OWNER)
        assert got["uses"] == 0 and got["confidence"] == 0.5

    def test_private_feedback_owner_ok(self, store: MemoryStore):
        priv = store.write("contract private feedback", type="fact", source=OWNER, ns=PRIVATE_NS)
        assert store.feedback(priv["id"], OWNER)["uses"] == 1
