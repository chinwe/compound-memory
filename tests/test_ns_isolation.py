"""ns 隔离行为测试（store 缝）：读写双侧属主边界 + 旁路通道（link/feedback/distill）。

spec 原为「读不隔离」，2026-10-03 修订为双侧属主校验（fail-closed，缺身份即拒绝）。
默认检索的候选圈定已挡住直读；这里钉的是显式 ns 与旁路的拒绝/放行，
以及 get 输出对跨 ns 遗留链的脱敏。MCP 参数透传在 test_mcp_tools.py。

为何要钉旁路：私有内容的主路（search/get）封死后，剩余攻击面是按 id
直操作的三条旁路——共同前提是拿到私有 id，所以 links 脱敏也是边界的一部分。
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from compound_memory.cli import main as cli_main
from compound_memory.storage import MemoryStore
from conftest import CLOCK_DATE

OWNER = "agent-zcode"
OWNER_BARE = "zcode"
FOREIGN = "agent-workbuddy"
PRIVATE_NS = "agent-zcode"


def _days_ago(n: int) -> str:
    """相对测试固定"今天"（CLOCK_DATE）推算——store 的 clock 已注入同一日期。"""
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


def _seed(store: MemoryStore) -> tuple[str, str]:
    """预置一对最简数据：属主私有记忆 + 外来 _shared 公开记忆。"""
    priv = store.write("zcode private draft", type="fact", source=OWNER, ns=PRIVATE_NS, key="priv")
    pub = store.write("shared public note", type="fact", source=FOREIGN, ns="_shared", key="pub")
    return priv["id"], pub["id"]


def _add_legacy_link(store: MemoryStore, mem_id: str, target_id: str) -> None:
    """伪造存量跨 ns 链（规则生效前的遗留数据）：改文件 links 后重建索引。

    正路 link() 现已拒绝跨 ns，跨 ns 链只能这样造出来——这正是测试目的。
    """
    mem = store.find(mem_id)
    assert mem is not None
    mem.links.append(target_id)
    store._save(mem)


class TestReadIsolation:
    def test_default_search_never_sees_private(self, store: MemoryStore):
        """主路隔离：_shared 检索即便用私有记忆的原词也命中不了私有 ns。"""
        _seed(store)
        hits = store.search("zcode private draft")
        assert all(h["ns"] != PRIVATE_NS for h in hits)

    def test_private_search_requires_owner_reader(self, store: MemoryStore):
        priv_id, _ = _seed(store)
        with pytest.raises(PermissionError):
            store.search("private draft", ns=PRIVATE_NS)
        with pytest.raises(PermissionError):
            store.search("private draft", ns=PRIVATE_NS, reader=FOREIGN)
        # 属主两种身份形式（全称/短名）都放行
        assert [h["id"] for h in store.search("private draft", ns=PRIVATE_NS, reader=OWNER)] == [priv_id]
        assert [h["id"] for h in store.search("private draft", ns=PRIVATE_NS, reader=OWNER_BARE)] == [priv_id]

    def test_shared_search_ignores_reader(self, store: MemoryStore):
        _seed(store)
        hits = store.search("public note", reader=FOREIGN)
        assert [h["ns"] for h in hits] == ["_shared"]

    def test_private_get_requires_owner_reader(self, store: MemoryStore):
        priv_id, _ = _seed(store)
        with pytest.raises(PermissionError):
            store.get(priv_id)
        with pytest.raises(PermissionError):
            store.get(priv_id, reader=FOREIGN)
        assert store.get(priv_id, reader=OWNER)["found"] is True
        assert store.get(priv_id, reader=OWNER_BARE)["found"] is True

    def test_legacy_cross_ns_link_redacted_from_get(self, store: MemoryStore):
        """存量跨 ns 链的双向脱敏：links 输出与邻居召回都不暴露对侧 id。"""
        priv_id, _ = _seed(store)
        bridge = store.write("bridging note", type="episode", source=FOREIGN)
        _add_legacy_link(store, bridge["id"], priv_id)
        _add_legacy_link(store, priv_id, bridge["id"])
        store.rebuild_index()

        got_pub = store.get(bridge["id"])
        assert got_pub["links"] == []
        assert got_pub.get("neighbors", []) == []
        got_priv = store.get(priv_id, reader=OWNER)
        assert got_priv["links"] == []

        hits = store.search("bridging")
        assert hits and hits[0]["neighbors"] == []


class TestLinkNamespaceRule:
    def test_cross_ns_link_rejected(self, store: MemoryStore):
        priv_id, pub_id = _seed(store)
        with pytest.raises(ValueError, match="across namespaces"):
            store.link(pub_id, priv_id)
        # 原子拒绝：两侧文件都不留下链
        assert store.find(pub_id) is not None and store.find(pub_id).links == []  # type: ignore[union-attr]
        assert store.find(priv_id) is not None and store.find(priv_id).links == []  # type: ignore[union-attr]

    def test_same_ns_link_ok(self, store: MemoryStore):
        _, pub_id = _seed(store)
        other = store.write("another shared note", type="fact", source=FOREIGN, ns="_shared")
        res = store.link(pub_id, other["id"])
        assert res["found"] is True
        assert other["id"] in store.find(pub_id).links  # type: ignore[union-attr]

    def test_self_link_still_rejected(self, store: MemoryStore):
        _, pub_id = _seed(store)
        with pytest.raises(ValueError, match="itself"):
            store.link(pub_id, pub_id)


class TestFeedbackNamespaceRule:
    def test_foreign_feedback_on_private_denied(self, store: MemoryStore):
        priv_id, _ = _seed(store)
        with pytest.raises(PermissionError):
            store.feedback(priv_id, FOREIGN)
        mem = store.find(priv_id)
        assert mem is not None and mem.uses == 0 and mem.confidence == 0.5

    def test_owner_feedback_on_private_ok(self, store: MemoryStore):
        priv_id, _ = _seed(store)
        res = store.feedback(priv_id, OWNER)
        assert res["uses"] == 1 and res["confidence"] == 0.6

    def test_shared_feedback_unaffected(self, store: MemoryStore):
        _, pub_id = _seed(store)
        assert store.feedback(pub_id, FOREIGN)["found"] is True


class TestDistillNamespaceRule:
    def test_cross_ns_sources_rejected_atomically(self, store: MemoryStore):
        priv_id, pub_id = _seed(store)
        # 私有 → _shared（正文外泄方向）与 _shared → 私有（越权并源）都拒绝
        with pytest.raises(ValueError, match="must live in target ns"):
            store.distill_apply("合并产物", type="insight", source=OWNER, source_ids=[priv_id], ns="_shared")
        with pytest.raises(ValueError, match="must live in target ns"):
            store.distill_apply("合并产物", type="insight", source=OWNER, source_ids=[pub_id], ns=PRIVATE_NS)
        # 原子：源未被归档、产物未落库
        assert store.find(priv_id) is not None and not store.find(priv_id).archived  # type: ignore[union-attr]
        assert store.find(pub_id) is not None and not store.find(pub_id).archived  # type: ignore[union-attr]

    def test_same_ns_distill_by_owner_ok(self, store: MemoryStore):
        priv_id, _ = _seed(store)
        res = store.distill_apply(
            "蒸馏产物内容", type="insight", source=OWNER, source_ids=[priv_id], ns=PRIVATE_NS
        )
        assert res["ns"] == PRIVATE_NS and priv_id in res["links"]
        assert store.find(priv_id) is not None and store.find(priv_id).archived  # type: ignore[union-attr]

    def test_distill_plan_private_requires_owner_reader(self, store: MemoryStore):
        """distill_plan 候选带正文返回：扫私有 ns 与 get/search 同规则。"""
        priv_id, _ = _seed(store)
        store.feedback(priv_id, OWNER)  # 过活性门（uses >= 1）
        with pytest.raises(PermissionError):
            store.distill_plan(ns=PRIVATE_NS)
        plan = store.distill_plan(ns=PRIVATE_NS, reader=OWNER)
        assert [c["id"] for c in plan["candidates"]] == [priv_id]


class TestReviveNamespaceRule:
    def test_revive_private_requires_owner_reader(self, store: MemoryStore):
        """revive 返回全文，与 get 同属按 id 读路径：私有 ns 仅属主可复活。"""
        old = store.write(
            "zcode old private episode", type="episode", source=OWNER, ns=PRIVATE_NS, created=_days_ago(120)
        )
        assert old["id"] in store.decay_sweep()
        with pytest.raises(PermissionError):
            store.revive(old["id"])
        assert store.revive(old["id"], reader=OWNER)["archived"] is False


class TestCliReaderFlag:
    """CLI 缺 reader 时 fail-closed（exit 2），--reader 全称/短名都放行。"""

    def test_cli_get_and_search_roundtrip(self, store: MemoryStore, capsys: pytest.CaptureFixture[str]):
        priv_id, _ = _seed(store)
        root = ["--root", str(store.root)]
        assert cli_main(root + ["search", "private draft", "--ns", PRIVATE_NS]) == 2
        assert cli_main(root + ["get", priv_id]) == 2
        assert cli_main(root + ["search", "private draft", "--ns", PRIVATE_NS, "--reader", OWNER]) == 0
        out = json.loads(capsys.readouterr().out)
        # CLI search 输出是裸数组（{"hits": ...} 包装只在 MCP 层）
        assert [h["id"] for h in out] == [priv_id]
        assert cli_main(root + ["get", priv_id, "--reader", OWNER_BARE]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["found"] is True
