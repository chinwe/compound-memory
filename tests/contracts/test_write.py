"""write 动词契约（store 层五要素 characterization）。

契约出处：#25（五要素模板）/ #26 P3（冲突判定谓词）/ P6（validity 校验）。
深层行为测试在 tests/test_input_validation.py 等模块测试；这里钉的是
「拆分/重构不得漂移」的外部契约，每条断言对当前实现跑绿。
"""

from __future__ import annotations

import re

import pytest

from compound_memory.storage import MemoryStore

from anchors import (
    FOREIGN,
    OWNER,
    OWNER_BARE,
    PRIVATE_NS,
    commit_count,
    last_message,
    matches,
)


class TestInputEquivalence:
    """输入等价类：合法落库的缺省形状 + 各非法参数类的错误类型（不穷举非法值）。"""

    def test_defaults_pinned_on_success(self, store: MemoryStore):
        """合法写入的缺省值：id 形如 YYYYMMDD_hex6、confidence 0.5、
        ns _shared、ttl 取自类型规格（episode=90）。"""
        mem = store.write("contract write default marker", type="episode", source="agent-a")
        assert re.fullmatch(r"\d{8}_[0-9a-f]{6}", mem["id"])
        assert mem["ns"] == "_shared"
        assert mem["confidence"] == 0.5
        assert mem["ttl"] == 90
        assert mem["conflict"] is False
        assert "conflicts_with" not in mem

    def test_bad_type_is_caller_error(self, store: MemoryStore):
        with pytest.raises(ValueError, match="type must be one of"):
            store.write("x", type="bogus", source="agent-a")

    @pytest.mark.parametrize("bad_key", ["BadKey", "bad key", "bad-", "-bad", "bad--key"])
    def test_bad_key_format_is_caller_error(self, store: MemoryStore, bad_key: str):
        """key 等价类：小写字母数字段以短横线连接合法；大写/空格/首尾横线/连续横线拒绝。"""
        with pytest.raises(ValueError, match="key must match"):
            store.write("x", type="fact", source="agent-a", key=bad_key)

    def test_bad_validity_dates_are_caller_error(self, store: MemoryStore):
        """P6：非 ISO 日期与 from>until 在 write 单点校验、响亮拒绝。"""
        with pytest.raises(ValueError, match="valid_until"):
            store.write("x", type="fact", source="agent-a", valid_until="2026/01/01")
        with pytest.raises(ValueError, match="valid_from"):
            store.write("x", type="fact", source="agent-a", valid_from="not-a-date")
        with pytest.raises(ValueError, match="after valid_until"):
            store.write("x", type="fact", source="agent-a", valid_from="2026-12-31", valid_until="2026-01-01")

    @pytest.mark.parametrize("bad_ns", ["shared", "Agent-X", "_Shared", "agent-../x", "agent x", "agent*"])
    def test_illegal_ns_is_caller_error(self, store: MemoryStore, bad_ns: str):
        """ns 等价类：白名单外字符（含路径穿越与 glob 元字符形态）与
        前缀不符（无 agent- 前缀/大写变体）都是 ValueError——且先于属主校验。"""
        with pytest.raises(ValueError):
            store.write("x", type="episode", source="agent-a", ns=bad_ns)


class TestPermissionMatrix:
    """权限矩阵（矩阵即测试表）：ns × source 的放行/拒绝；拒绝是原子的。"""

    @pytest.mark.parametrize("identity", [OWNER, OWNER_BARE], ids=["full-form", "bare-form"])
    def test_owner_writes_private_ns(self, store: MemoryStore, identity: str):
        mem = store.write("contract private write", type="fact", source=identity, ns=PRIVATE_NS)
        assert mem["ns"] == PRIVATE_NS

    def test_shared_ns_needs_no_identity(self, store: MemoryStore):
        assert store.write("contract shared write", type="episode", source=FOREIGN)["ns"] == "_shared"

    def test_foreign_writer_denied_atomically(self, store: MemoryStore):
        """越权写私有 ns：PermissionError + 零提交 + 目标 ns 目录不落地。"""
        before = commit_count(store)
        with pytest.raises(PermissionError, match="private"):
            store.write("x", type="episode", source=FOREIGN, ns=PRIVATE_NS)
        assert commit_count(store) == before
        assert not (store.ns_root / PRIVATE_NS).exists()


class TestSideEffects:
    """side effects 锚点：提交消息模板（#31）、文件落点、活动必被索引。"""

    def test_commits_with_template_message(self, store: MemoryStore):
        mem = store.write("contract commit anchor", type="fact", source="agent-a", key="anchor")
        m = matches("write", last_message(store))
        assert m["id"] == mem["id"]
        assert m["type"] == "fact"
        assert m["ns"] == "_shared"
        assert m["source"] == "agent-a"

    def test_lands_at_active_path(self, store: MemoryStore):
        """文件落点：活动区 namespaces/<ns>/<type>/<id>.md。"""
        mem = store.write("contract path anchor", type="insight", source="agent-a")
        assert (store.ns_root / "_shared" / "insight" / f"{mem['id']}.md").exists()

    def test_active_memory_is_indexed(self, store: MemoryStore):
        """既有不变量：活动记忆必被索引——写后即可检索。"""
        mem = store.write("contract invariant indexed marker", type="fact", source="agent-a")
        assert [h["id"] for h in store.search("invariant indexed marker", top_k=10)] == [mem["id"]]


class TestConflictEnqueue:
    """P3 冲突判定完整谓词：同 ns ∧ 同 type（key_conflicts 标记类型：
    fact/insight/decision，#46 表驱动化）∧ 同 key ∧ content.strip() 不等
    ⇒ 入队；episode/skill append-only 不判；跨 ns 不判。
    【契约变更 #46】冲突类型面从硬编码 (fact, insight) 扩为 TYPE_SPEC 表驱动，
    decision 加入 key 更新通道（同 key 冲突照 fact/insight 入队）——类型面
    扩展属显式契约变更，判定谓词其余各维（ns/key/content）一条未动。"""

    def test_same_key_fact_conflict_enqueues(self, store: MemoryStore):
        first = store.write("vercel timeout is 10s", type="fact", source="agent-a", key="vt")
        second = store.write("vercel timeout is 60s", type="fact", source="agent-b", key="vt")
        assert second["conflict"] is True
        assert second["conflicts_with"] == first["id"]
        assert len(store.review_queue()) == 1

    def test_same_key_decision_conflict_enqueues(self, store: MemoryStore):
        """【#46 新增】decision 走同 key 冲突通道：冲突决策与冲突事实同进入队裁决。"""
        first = store.write("engine is sqlite-vec", type="decision", source="agent-a", key="vec-engine")
        second = store.write("engine is pgvector", type="decision", source="agent-b", key="vec-engine")
        assert second["conflict"] is True
        assert second["conflicts_with"] == first["id"]
        assert len(store.review_queue()) == 1

    def test_same_content_same_key_is_not_conflict(self, store: MemoryStore):
        store.write("python 3.13 is current", type="fact", source="agent-a", key="py")
        again = store.write("python 3.13 is current", type="fact", source="agent-b", key="py")
        assert again["conflict"] is False
        assert store.review_queue() == []

    def test_episode_same_key_never_conflicts(self, store: MemoryStore):
        """episode append-only：同 key 不同内容不判冲突（key 更新通道仅限
        key_conflicts 标记类型：fact/insight/decision）。"""
        store.write("deploy log day one", type="episode", source="agent-a", key="deploy")
        second = store.write("deploy log day two", type="episode", source="agent-a", key="deploy")
        assert second["conflict"] is False

    def test_conflict_is_intra_namespace(self, store: MemoryStore):
        """冲突判定含 ns 维度：shared 与私有 ns 的同 key 事实互不冲突。"""
        store.write("shared lead is alice", type="fact", source=FOREIGN, key="lead")
        priv = store.write("private lead is bob", type="fact", source=OWNER, ns=PRIVATE_NS, key="lead")
        assert priv["conflict"] is False
