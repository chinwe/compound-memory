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


class TestLinkOwnershipGate:
    """D1（#34，2026-10-06 决议）：link 是最后一个无身份写入口——
    同 ns 私有记忆的 link 仅属主（可选 agent 参数，对称 feedback），按 id 动词
    在锁内 find 后过门；跨 ns 拒绝语义在前、不变（见 TestLinkNamespaceRule）。
    _shared 的 link 无需身份（写侧本就不设防，与 write 一致）。"""

    def _two_private(self, store: MemoryStore) -> tuple[str, str]:
        a = store.write("zcode 私有关联甲", type="fact", source=OWNER, ns=PRIVATE_NS)
        b = store.write("zcode 私有关联乙", type="fact", source=OWNER, ns=PRIVATE_NS)
        return a["id"], b["id"]

    def test_private_link_without_identity_denied(self, store: MemoryStore):
        """fail-closed：私有 ns 缺身份即拒绝（宁可不连，不猜身份），且原子——不落半条链。"""
        a, b = self._two_private(store)
        with pytest.raises(PermissionError):
            store.link(a, b)
        assert store.find(a) is not None and store.find(a).links == []  # type: ignore[union-attr]
        assert store.find(b) is not None and store.find(b).links == []  # type: ignore[union-attr]

    def test_private_link_foreign_agent_denied(self, store: MemoryStore):
        a, b = self._two_private(store)
        with pytest.raises(PermissionError):
            store.link(a, b, agent=FOREIGN)
        assert store.find(a) is not None and store.find(a).links == []  # type: ignore[union-attr]

    def test_private_link_owner_ok_both_identity_forms(self, store: MemoryStore):
        """属主全称/短名都放行；返回形状不变（found/a/b/links）。"""
        a, b = self._two_private(store)
        assert store.link(a, b, agent=OWNER)["found"] is True
        c = store.write("zcode 私有关联丙", type="fact", source=OWNER, ns=PRIVATE_NS)
        d = store.write("zcode 私有关联丁", type="fact", source=OWNER, ns=PRIVATE_NS)
        res = store.link(c["id"], d["id"], agent=OWNER_BARE)
        assert res == {"found": True, "a": c["id"], "b": d["id"], "links": [d["id"]]}
        assert d["id"] in store.find(c["id"]).links  # type: ignore[union-attr]

    def test_attested_private_link_autofills_agent(self, attested: MemoryStore):
        """进程身份已证明时缺省自动补真值；伪造 agent 与进程矛盾响亮拒绝。"""
        a = attested.write("zcode 私有关联戊", type="fact", source=OWNER, ns=PRIVATE_NS)
        b = attested.write("zcode 私有关联己", type="fact", source=OWNER, ns=PRIVATE_NS)
        assert attested.link(a["id"], b["id"])["found"] is True  # 忘带 agent：自动补进程身份
        c = attested.write("zcode 私有关联庚", type="fact", source=OWNER, ns=PRIVATE_NS)
        d = attested.write("zcode 私有关联辛", type="fact", source=OWNER, ns=PRIVATE_NS)
        with pytest.raises(PermissionError, match="attested agent"):
            attested.link(c["id"], d["id"], agent=FOREIGN)

    def test_missing_ids_still_reported_before_gate(self, store: MemoryStore):
        """按 id 动词信封约定不变：目标不存在返回 found:False（门禁在其后）。"""
        a, _ = self._two_private(store)
        res = store.link(a, "nope", agent=OWNER)
        assert res == {"found": False, "missing": ["nope"]}


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


class TestReviewResolvePrivateGate:
    """D2（#34，2026-10-06 决议）：清行按属主可见性收口——agent-* 私有 ns 的行
    仅属主可 resolve（可选 reader 参数），_shared 行不受影响、现有运维流程不变；
    review-queue 展示维持全量（张力：CLI 是本机信任边界，队列行含 content[:40] 片段，
    MCP 5 tool 不暴露队列——契约文档明示）。

    私有行的属主裁决以行内 ns 为准（冲突同 ns 产生，行格式自带 ns/type/key），
    不依赖记忆文件是否仍在——行是登记事实，清行许可见 ns 即可判。"""

    def _private_conflict(self, store: MemoryStore, key: str = "priv-conflict") -> tuple[str, str]:
        """属主私有 ns 内制造一对同 key fact 冲突，返回 (old_id, new_id)。"""
        old = store.write(f"zcode 私有事实旧版 {key}", type="fact", source=OWNER, ns=PRIVATE_NS, key=key)
        new = store.write(f"zcode 私有事实新版 {key}", type="fact", source=OWNER, ns=PRIVATE_NS, key=key)
        return old["id"], new["id"]

    def test_display_stays_full(self, store: MemoryStore):
        """展示维持全量：无身份调用方 review_queue() 仍列出私有行（清行收口、展示不收）。"""
        old, new = self._private_conflict(store)
        lines = store.review_queue()
        assert len(lines) == 1 and old in lines[0] and new in lines[0]

    def test_resolve_private_row_without_reader_denied_atomically(self, store: MemoryStore):
        """显式点名私有行：缺身份/外来身份均 PermissionError 原子拒绝，队列原样保留。"""
        old, _ = self._private_conflict(store)
        with pytest.raises(PermissionError):
            store.review_resolve([old])
        with pytest.raises(PermissionError):
            store.review_resolve([old], reader=FOREIGN)
        assert len(store.review_queue()) == 1

    def test_resolve_private_row_by_owner(self, store: MemoryStore):
        """属主清私有行 + 自动归档废置方：与 _shared 行为同构，--reader 全称/短名皆可。"""
        old, _ = self._private_conflict(store)
        out = store.review_resolve([old], reader=OWNER_BARE)
        assert out["resolved"] == 1 and out["archived"] == [old]
        assert store.review_queue() == []
        assert store.get(old, reader=OWNER)["archived"] is True

    def test_all_filters_private_rows_for_caller_without_identity(self, store: MemoryStore):
        """--all 是清行动作，同样按属主可见性过滤：无身份只清 _shared 行，私有行保留。"""
        self._private_conflict(store)
        pub_old = store.write("共享事实旧版", type="fact", source=FOREIGN, key="pub-conflict")["id"]
        store.write("共享事实新版", type="fact", source=FOREIGN, key="pub-conflict")
        out = store.review_resolve(all=True)
        assert out["resolved"] == 1 and out["remaining"] == 1
        remaining = store.review_queue()
        assert len(remaining) == 1 and PRIVATE_NS in remaining[0] and pub_old not in remaining[0]

    def test_all_with_owner_reader_clears_own_private_rows(self, store: MemoryStore):
        """--all 带 --reader 属主：自有私有行照清；--all 不归档的既有语义不变。"""
        self._private_conflict(store)
        out = store.review_resolve(all=True, reader=OWNER)
        assert out == {"resolved": 1, "remaining": 0, "archived": []}

    def test_shared_rows_unaffected_without_reader(self, store: MemoryStore):
        """回归护栏：无身份时 _shared 行的 resolve（按 id 与 --all）行为与旧版完全一致。"""
        old = store.write("共享事实旧版", type="fact", source=FOREIGN, key="k1")["id"]
        store.write("共享事实新版", type="fact", source=FOREIGN, key="k1")
        assert store.review_resolve([old])["archived"] == [old]
        store.write("共享事实旧版二", type="fact", source=FOREIGN, key="k2")
        store.write("共享事实新版二", type="fact", source=FOREIGN, key="k2")
        assert store.review_resolve(all=True) == {"resolved": 1, "remaining": 0, "archived": []}

    def test_attested_autofills_reader(self, attested: MemoryStore):
        """进程身份注入：缺省 reader 自动补真值，自有私有行可清，外来私有行保留。

        外来 ns 的冲突对经 _write_new 播种（attested store 上 write 的 source
        须与进程身份一致，播不进外来 ns——正是既有语义）。"""
        self._private_conflict(attested, key="att-conflict")
        attested._write_new(
            "workbuddy 私有冲突旧版", type="fact", source=FOREIGN, ns="agent-workbuddy",
            key="fw", links=None, created=None, confidence=None, origin=None,
        )
        foreign_new = attested._write_new(
            "workbuddy 私有冲突新版", type="fact", source=FOREIGN, ns="agent-workbuddy",
            key="fw", links=None, created=None, confidence=None, origin=None,
        )[0]
        out = attested.review_resolve(all=True)
        assert out["resolved"] == 1 and out["remaining"] == 1
        remaining = attested.review_queue()
        assert len(remaining) == 1 and foreign_new.id in remaining[0]


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


class TestCliIdentityFlags:
    """D1/D2 的 CLI 缝：link --agent 与 review-resolve --reader 透传属主身份。

    缺身份 fail-closed（exit 2 + stderr JSON，同 get/search 的 --reader 约定）；
    _shared 操作不带 flag 行为不变。"""

    def test_cli_link_private_requires_agent(self, store: MemoryStore, capsys: pytest.CaptureFixture[str]):
        a = store.write("zcode 私有关联甲", type="fact", source=OWNER, ns=PRIVATE_NS)["id"]
        b = store.write("zcode 私有关联乙", type="fact", source=OWNER, ns=PRIVATE_NS)["id"]
        root = ["--root", str(store.root)]
        assert cli_main(root + ["link", a, b]) == 2
        err = json.loads(capsys.readouterr().err)
        assert "private" in err["error"]
        assert cli_main(root + ["link", a, b, "--agent", OWNER_BARE]) == 0
        assert json.loads(capsys.readouterr().out)["found"] is True

    def test_cli_review_resolve_private_requires_reader(self, store: MemoryStore, capsys: pytest.CaptureFixture[str]):
        old = store.write(
            "zcode 私有事实旧版", type="fact", source=OWNER, ns=PRIVATE_NS, key="cli-conflict"
        )["id"]
        store.write("zcode 私有事实新版", type="fact", source=OWNER, ns=PRIVATE_NS, key="cli-conflict")
        root = ["--root", str(store.root)]
        assert cli_main(root + ["review-resolve", old]) == 2
        err = json.loads(capsys.readouterr().err)
        assert "private" in err["error"]
        assert len(store.review_queue()) == 1  # 原子拒绝，队列原样
        assert cli_main(root + ["review-resolve", old, "--reader", OWNER]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["resolved"] == 1 and out["archived"] == [old]


class TestLexicalCandidatesGate:
    """公开词面候选通道（extraction 复述标注走此正门，替代 _candidates 私有直调）：
    与 search 同一门禁——agent-* 必须属主，_shared 放行。"""

    def test_private_ns_requires_owner(self, store: MemoryStore):
        from compound_memory.scoring import tokenize

        _seed(store)
        with pytest.raises(PermissionError):
            store.lexical_candidates(tokenize("zcode private draft"), {PRIVATE_NS})
        hits = store.lexical_candidates(tokenize("zcode private draft"), {PRIVATE_NS}, reader=OWNER)
        assert [m.id for m in hits]
        shared = store.lexical_candidates(tokenize("shared public note"), {"_shared"})
        assert [m.id for m in shared]
