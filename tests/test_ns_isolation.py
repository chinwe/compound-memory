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


class TestDualChannelSearch:
    """默认检索双通道（ns 缺省 = _shared ∪ 自有私有 ns）。

    动机：私有条目被 ns 硬隔离后，调用方"忘记补搜私有 ns"已两次造成漏召回
    （问自身称呼答错名字）。保障下沉到检索层：身份已知时默认搜索自动并入
    自有私有 ns，不依赖调用方记得；身份未知退化为单 _shared（与旧版一致）；
    显式传 ns 永远是单 ns 精确语义（含显式 _shared 不带私有）。
    """

    def test_attested_default_search_spans_shared_and_own_private(self, attested: MemoryStore):
        attested.write("zcode 独有偏好", type="fact", source=OWNER, ns=PRIVATE_NS)
        shared = attested.write("zcode 共享笔记", type="fact", source=OWNER)
        foreign_mem, _ = attested._write_new(
            "zcode 他家隐私", type="fact", source=FOREIGN, ns="agent-workbuddy",
            key=None, links=None, created=None, confidence=None, origin=None,
        )
        hits = attested.search("zcode")  # 不传 ns：_shared ∪ agent-zcode
        hit_ids = [h["id"] for h in hits]
        assert shared["id"] in hit_ids
        assert any(h["ns"] == PRIVATE_NS for h in hits)
        assert foreign_mem.id not in hit_ids  # 别人的私有 ns 不因双通道而可见
        assert all(h["ns"] in ("_shared", PRIVATE_NS) for h in hits)

    def test_attested_reader_bare_form_same_channel(self, attested: MemoryStore):
        """reader 短名（zcode）与全称（agent-zcode）派生出同一私有通道。"""
        priv = attested.write("zcode 私有草稿", type="fact", source=OWNER, ns=PRIVATE_NS)
        full = [h["id"] for h in attested.search("私有草稿", reader=OWNER)]
        bare = [h["id"] for h in attested.search("私有草稿", reader=OWNER_BARE)]
        assert full == bare == [priv["id"]]

    def test_unattested_default_search_stays_shared(self, store: MemoryStore):
        """回归护栏：无身份时默认检索与旧版完全一致（只搜 _shared）。"""
        _seed(store)
        hits = store.search("private draft")
        assert all(h["ns"] == "_shared" for h in hits)

    def test_explicit_shared_ns_excludes_private_even_attested(self, attested: MemoryStore):
        """显式 ns 是精确语义：显式 _shared 即便有身份也不并私有通道。"""
        attested.write("zcode 私有草稿", type="fact", source=OWNER, ns=PRIVATE_NS)
        hits = attested.search("私有草稿", ns="_shared")
        assert hits == []

    def test_neighbor_recall_follows_dual_channel_for_owner(self, attested: MemoryStore):
        """邻居召回的 ns 过滤同样走通道集合：属主默认搜索可带出自有私有邻居，
        别人的私有邻居不可见（跨 ns 链只能以遗留数据方式存在）。"""
        priv = attested.write("zcode 私有关联笔记", type="fact", source=OWNER, ns=PRIVATE_NS)
        bridge = attested.write("zcode 桥接笔记", type="episode", source=OWNER)
        foreign_mem, _ = attested._write_new(
            "workbuddy 私有关联", type="fact", source=FOREIGN, ns="agent-workbuddy",
            key=None, links=None, created=None, confidence=None, origin=None,
        )
        _add_legacy_link(attested, bridge["id"], priv["id"])
        _add_legacy_link(attested, bridge["id"], foreign_mem.id)
        attested.rebuild_index()
        hits = attested.search("桥接笔记")
        # 「笔记」bigram 也会直接命中私有记忆本体（双通道预期内），按 id 定位桥接条
        bridge_hit = next(h for h in hits if h["id"] == bridge["id"])
        neighbor_ids = [n["id"] for n in bridge_hit["neighbors"]]
        assert priv["id"] in neighbor_ids
        assert foreign_mem.id not in neighbor_ids


@pytest.fixture
def attested(tmp_path) -> MemoryStore:
    """进程身份已证明的 store：TestAttestation 与 TestDualChannelSearch 共用。"""
    from conftest import sandbox_safe_remove

    return MemoryStore(
        tmp_path / "attested", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove, agent_id=OWNER
    )


class TestAttestation:
    """进程侧身份证明：身份等于进程（COMPOUND_MEMORY_AGENT_ID），不等于自称。

    三条裁决规则：缺省自动补进程身份（诚实缺省）、等价形式归一化、
    矛盾响亮报错——连带关闭「_shared 伪造 source 污染跨 Agent 验证」的口子。
    """

    def test_reader_auto_filled_for_owner_ns(self, attested: MemoryStore):
        priv = attested.write("私有草稿", type="fact", source=OWNER, ns=PRIVATE_NS)
        hits = attested.search("私有草稿", ns=PRIVATE_NS)  # 忘带 reader：自动补进程身份
        assert [h["id"] for h in hits] == [priv["id"]]

    def test_forged_reader_rejected(self, attested: MemoryStore):
        attested.write("私有草稿", type="fact", source=OWNER, ns=PRIVATE_NS)
        with pytest.raises(PermissionError, match="attested agent"):
            attested.search("私有草稿", ns=PRIVATE_NS, reader=FOREIGN)

    def test_foreign_ns_denied_even_with_autofill(self, attested: MemoryStore):
        """自动补的是进程身份：读别人的私有 ns 依旧被属主门挡住。"""
        mem, _ = attested._write_new(
            "workbuddy 私密",
            type="fact",
            source=FOREIGN,
            ns="agent-workbuddy",
            key=None,
            links=None,
            created=None,
            confidence=None,
            origin=None,
        )
        with pytest.raises(PermissionError):
            attested.search("workbuddy 私密", ns="agent-workbuddy")
        with pytest.raises(PermissionError):
            attested.get(mem.id)

    def test_write_source_must_match_process(self, attested: MemoryStore):
        """attestation 连带关闭 _shared 伪造 source 的口子；等价形式归一化。"""
        with pytest.raises(PermissionError, match="attested agent"):
            attested.write("x", type="fact", source=FOREIGN)
        res = attested.write("y", type="fact", source="zcode")  # 短名等价
        assert res["source"] == OWNER  # 落库归一化为进程身份

    def test_feedback_agent_must_match_process(self, attested: MemoryStore):
        mem = attested.write("x", type="fact", source=OWNER)
        with pytest.raises(PermissionError, match="attested agent"):
            attested.feedback(mem["id"], FOREIGN)
        assert attested.feedback(mem["id"], "zcode")["validated_by"] == [OWNER]

    def test_unattested_keeps_self_declared(self, store: MemoryStore):
        """回归护栏：未启用 attestation 的 store 行为与旧版完全一致。"""
        assert store.write("x", type="fact", source=FOREIGN)["source"] == FOREIGN

    def test_cli_env_attestation(self, store: MemoryStore, monkeypatch, capsys):
        priv_id, _ = _seed(store)
        monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", OWNER)
        root = ["--root", str(store.root)]
        assert cli_main(root + ["get", priv_id]) == 0  # reader 自动补进程身份
        capsys.readouterr()
        assert cli_main(root + ["get", priv_id, "--reader", FOREIGN]) == 2


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
