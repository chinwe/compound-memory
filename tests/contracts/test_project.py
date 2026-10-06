"""project 作用域维度契约（ADR 0010 / #51 的 store 层 characterization）。

契约出处：#49 裁决（ADR-0010）——可选 frontmatter 字段 `project`（小写 slug，
复用 key 格式正则，校验单点 validation，绝不进落盘路径）；检索硬过滤缺省
fail-closed（不传 project 只见全局记忆，传了见 全局 ∪ 该项目）；词面/向量/
邻居三路候选同一过滤；get 按 id 恒可读、邻居带出按读方 project 滤除（ns
脱敏先例）；蒸馏产物继承源的 project；link 不限 project；蒸馏/衰减等库级
动词不过滤；私有 ns ∩ project 两级门依次收窄（先可见性后适用性）。
"""

from __future__ import annotations

import pytest

from compound_memory.storage import MemoryStore

from anchors import FOREIGN, OWNER, OWNER_BARE, PRIVATE_NS

PROJECT = "agenthub"
OTHER = "webmail"


class TestWriteSide:
    """写侧：slug 校验、frontmatter round-trip、落盘路径不变。"""

    @pytest.mark.parametrize(
        "bad_project",
        ["Project-X", "proj x", "proj-", "-proj", "proj--x", "", "proj_x", "../escape", "agent*"],
    )
    def test_bad_slug_is_caller_error(self, store: MemoryStore, bad_project: str):
        """slug 等价类：小写字母数字段以短横线连接合法；大写/空格/首尾或连续
        横线/下划线/穿越与 glob 元字符拒绝——复用 key 格式正则，报错响亮。"""
        with pytest.raises(ValueError, match="project must match"):
            store.write("x", type="fact", source="agent-a", project=bad_project)

    def test_project_round_trips_through_frontmatter(self, store: MemoryStore):
        """project 落 frontmatter（可选字段）：写侧显式传参 → 结果与重读都携带；
        未传时为 None 且不落盘（save 的空值剔除）。"""
        mem = store.write("contract project marker", type="fact", source="agent-a", project=PROJECT)
        assert mem["project"] == PROJECT
        again = store.find(mem["id"])
        assert again is not None and again.project == PROJECT
        plain = store.write("contract global marker", type="fact", source="agent-a")
        assert plain["project"] is None
        assert "project" not in (store.ns_root / "_shared" / "fact" / f"{plain['id']}.md").read_text(
            encoding="utf-8"
        )

    def test_project_never_enters_disk_path(self, store: MemoryStore):
        """落盘路径不变量：project 是纯元数据，文件仍在 namespaces/<ns>/<type>/。"""
        mem = store.write("contract path anchor", type="fact", source="agent-a", project=PROJECT)
        assert (store.ns_root / "_shared" / "fact" / f"{mem['id']}.md").exists()
        assert not (store.root / "projects").exists()


class TestFailClosedDefault:
    """检索硬过滤（ADR 0010 核心）：没声明项目 = 全局会话，项目记忆不外溢。"""

    def test_without_project_only_global_visible(self, store: MemoryStore):
        glob = store.write("agenthub deploy marker", type="fact", source=FOREIGN)
        store.write("agenthub deploy marker", type="fact", source=FOREIGN, project=PROJECT)
        hits = store.search("agenthub deploy marker", top_k=10)
        assert [h["id"] for h in hits] == [glob["id"]]

    def test_with_project_sees_global_union_project(self, store: MemoryStore):
        glob = store.write("agenthub deploy marker", type="fact", source=FOREIGN)
        proj = store.write("agenthub deploy marker", type="fact", source=FOREIGN, project=PROJECT)
        hits = store.search("agenthub deploy marker", top_k=10, project=PROJECT)
        # 断言可见性集合而非次序：同内容记忆分数并列，次序属 rank 的 tie-break 细节
        assert {h["id"] for h in hits} == {proj["id"], glob["id"]}

    def test_other_project_does_not_see_foreign_project(self, store: MemoryStore):
        """项目隔离是双向的：声明 project=B 的读方看不见 project=A 的记忆。"""
        store.write("agenthub deploy marker", type="fact", source=FOREIGN, project=PROJECT)
        assert store.search("agenthub deploy marker", top_k=10, project=OTHER) == []

    def test_malformed_project_param_is_caller_error_not_silent_empty(self, store: MemoryStore):
        """与非法 ns 同规：拼错的 slug 必须抛 ValueError——静默退化成全局检索
        会让调用方误判「无相关记忆」。"""
        with pytest.raises(ValueError, match="project must match"):
            store.search("agenthub deploy marker", project="Not A Slug")


class TestThreeChannels:
    """过滤收口在候选层单点：词面 / 向量 KNN / 邻居三路同过滤。"""

    def test_lexical_channel_filters(self, store: MemoryStore):
        store.write("agenthub deploy marker", type="fact", source=FOREIGN, project=PROJECT)
        assert store.search("agenthub deploy marker", top_k=10) == []
        assert len(store.search("agenthub deploy marker", top_k=10, project=PROJECT)) == 1

    def test_vector_channel_filters(self, vec_store: MemoryStore):
        """向量召回不构成旁路：KNN 取回的项目记忆在候选层被同一谓词滤除。"""
        vec_store.write("kubernetes ingress routing notes", type="fact", source=FOREIGN, project=PROJECT)
        assert vec_store.search("kubernetes ingress routing", top_k=10) == []
        assert len(vec_store.search("kubernetes ingress routing", top_k=10, project=PROJECT)) == 1

    def test_neighbor_ride_along_filters_in_search(self, store: MemoryStore):
        """search 命中的邻居带出按读方 project 滤除（全局记忆是枢纽的常态形态）。

        查询 token 与侧条目部分重叠，故按锚点 id 取 hit 断言邻居（可见性集合
        语义在 TestFailClosedDefault 已钉）。"""
        glob = store.write("agenthub deploy marker", type="fact", source=FOREIGN)
        proj_side = store.write("agenthub deploy sidebar", type="fact", source=FOREIGN, project=PROJECT)
        glob_side = store.write("agenthub deploy public", type="fact", source=FOREIGN)
        store.link(glob["id"], proj_side["id"])
        store.link(glob["id"], glob_side["id"])
        (hit,) = [h for h in store.search("agenthub deploy marker", top_k=10) if h["id"] == glob["id"]]
        assert [n["id"] for n in hit["neighbors"]] == [glob_side["id"]]
        (hit,) = [h for h in store.search("agenthub deploy marker", top_k=10, project=PROJECT) if h["id"] == glob["id"]]
        assert [n["id"] for n in hit["neighbors"]] == [proj_side["id"], glob_side["id"]]

    def test_lexical_candidates_gate(self, store: MemoryStore):
        """extraction 复述标注的公开词面通道过同套门禁（不传 project 只见全局）。"""
        proj = store.write("agenthub deploy marker", type="fact", source=FOREIGN, project=PROJECT)
        glob = store.write("agenthub deploy marker two", type="fact", source=FOREIGN)
        from compound_memory.scoring import tokenize

        tokens = tokenize("agenthub deploy marker")
        assert [m.id for m in store.lexical_candidates(tokens, {"_shared"})] == [glob["id"]]
        got = store.lexical_candidates(tokens, {"_shared"}, project=PROJECT)
        assert proj["id"] in [m.id for m in got] and glob["id"] in [m.id for m in got]


class TestGetSemantics:
    """get 按 id 恒可读（显式寻址不受限，valid_until 先例）；邻居带出按读方
    project 滤除；links 仅 id 列表不滤（适用性轴不脱敏，内容由邻居过滤兜底）。"""

    def test_get_by_id_always_readable(self, store: MemoryStore):
        proj = store.write("agenthub deploy marker", type="fact", source=FOREIGN, project=PROJECT)
        assert store.get(proj["id"])["found"] is True
        assert store.get(proj["id"], project=OTHER)["found"] is True

    def test_get_neighbors_filtered_by_reader_project(self, store: MemoryStore):
        glob = store.write("agenthub deploy marker", type="fact", source=FOREIGN)
        proj_side = store.write("agenthub deploy sidebar", type="fact", source=FOREIGN, project=PROJECT)
        glob_side = store.write("agenthub deploy public", type="fact", source=FOREIGN)
        store.link(glob["id"], proj_side["id"])
        store.link(glob["id"], glob_side["id"])
        got = store.get(glob["id"])
        assert [n["id"] for n in got["neighbors"]] == [glob_side["id"]]
        got = store.get(glob["id"], project=PROJECT)
        assert [n["id"] for n in got["neighbors"]] == [proj_side["id"], glob_side["id"]]

    def test_get_links_survive_cross_project(self, store: MemoryStore):
        """links 是 id 列表（无正文）：适用性轴不脱敏，与 ns 脱敏（可见性轴）不同。"""
        glob = store.write("agenthub deploy marker", type="fact", source=FOREIGN)
        proj_side = store.write("agenthub deploy sidebar", type="fact", source=FOREIGN, project=PROJECT)
        store.link(glob["id"], proj_side["id"])
        assert store.get(glob["id"])["links"] == [proj_side["id"]]


class TestDistillInheritance:
    """蒸馏产物继承源的 project（同项目提纯）；库级动词不过滤。"""

    def test_product_inherits_source_project(self, store: MemoryStore):
        s1 = store.write("agenthub deploy fact one", type="fact", source=FOREIGN, project=PROJECT)
        s2 = store.write("agenthub deploy fact two", type="fact", source=FOREIGN, project=PROJECT)
        out = store.distill_apply("agenthub deploy merged", type="insight", source="agent-zcode",
                                  source_ids=[s1["id"], s2["id"]], key="deploy-merged")
        assert out["project"] == PROJECT

    def test_product_inherits_annotated_source_over_global(self, store: MemoryStore):
        """全局源 ⊕ 项目源：产物取已标注值（可见面收窄不泄漏——全局产物会把
        项目源内容泄进全局会话）。"""
        s1 = store.write("agenthub deploy fact one", type="fact", source=FOREIGN)
        s2 = store.write("agenthub deploy fact two", type="fact", source=FOREIGN, project=PROJECT)
        out = store.distill_apply("agenthub deploy merged", type="insight", source="agent-zcode",
                                  source_ids=[s1["id"], s2["id"]], key="deploy-merged")
        assert out["project"] == PROJECT

    def test_conflicting_project_sources_rejected(self, store: MemoryStore):
        """两个不同项目源无单一适用域：显式拒绝（与跨 ns 拒绝同型）。"""
        s1 = store.write("agenthub deploy fact one", type="fact", source=FOREIGN, project=PROJECT)
        s2 = store.write("agenthub deploy fact two", type="fact", source=FOREIGN, project=OTHER)
        with pytest.raises(ValueError, match="project scope"):
            store.distill_apply("agenthub deploy merged", type="insight", source="agent-zcode",
                                source_ids=[s1["id"], s2["id"]], key="deploy-merged")

    def test_distill_plan_and_decay_stay_library_level(self, store: MemoryStore):
        """库级动词不过滤：项目记忆照常进蒸馏候选与衰减归档（否则复利环断裂）。"""
        proj = store.write("agenthub deploy marker", type="episode", source=FOREIGN, project=PROJECT)
        plan = store.distill_plan(min_uses=0)  # min_uses=0 绕开活性门，只验 project 不过滤
        assert proj["id"] in [c["id"] for c in plan["candidates"]]
        old = store.write("agenthub stale episode", type="episode", source=FOREIGN, project=PROJECT,
                          created="2026-05-01")
        assert old["id"] in store.decay_sweep()


class TestLinkUnrestricted:
    """link 不限 project（同 ns 规则不变）：全局 ↔ 项目互链是常态。"""

    def test_global_and_project_memories_link(self, store: MemoryStore):
        glob = store.write("agenthub deploy marker", type="fact", source=FOREIGN)
        proj = store.write("agenthub deploy sidebar", type="fact", source=FOREIGN, project=PROJECT)
        assert store.link(glob["id"], proj["id"])["found"] is True

    def test_cross_project_memories_link(self, store: MemoryStore):
        a = store.write("agenthub deploy marker", type="fact", source=FOREIGN, project=PROJECT)
        b = store.write("agenthub deploy sidebar", type="fact", source=FOREIGN, project=OTHER)
        assert store.link(a["id"], b["id"])["found"] is True


class TestPrivateNsIntersection:
    """私有 ns ∩ project：agent-* 记忆同样可标 project；两级门依次收窄——
    缺省双通道先圈可见性（ns），project 过滤再圈适用性。"""

    @pytest.mark.parametrize("reader", [OWNER, OWNER_BARE], ids=["full-form", "bare-form"])
    def test_private_project_memory_needs_both_gates(self, store: MemoryStore, reader: str):
        priv = store.write("agenthub private draft", type="fact", source=reader, ns=PRIVATE_NS,
                           project=PROJECT)
        # 只过可见性门（reader=属主、无 project）⇒ 适用性门把项目记忆滤除
        assert store.search("agenthub private draft", reader=reader, top_k=10) == []
        # 两级门都过 ⇒ 可见
        hits = store.search("agenthub private draft", reader=reader, top_k=10, project=PROJECT)
        assert [h["id"] for h in hits] == [priv["id"]]
        # 显式私有 ns 同语义
        assert store.search("agenthub private draft", ns=PRIVATE_NS, reader=reader, top_k=10) == []
        hits = store.search("agenthub private draft", ns=PRIVATE_NS, reader=reader, top_k=10,
                            project=PROJECT)
        assert [h["id"] for h in hits] == [priv["id"]]

    def test_private_project_memory_invisible_without_identity(self, store: MemoryStore):
        """可见性门先行：无身份连私有通道都进不去，project 语义无从谈起。"""
        store.write("agenthub private draft", type="fact", source=OWNER, ns=PRIVATE_NS, project=PROJECT)
        glob = store.write("agenthub shared note", type="fact", source=FOREIGN)
        hits = store.search("agenthub", top_k=10, project=PROJECT)
        assert [h["id"] for h in hits] == [glob["id"]]
