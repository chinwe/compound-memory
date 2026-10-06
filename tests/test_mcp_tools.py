"""MCP tool 边界测试——spec 约定的主测试缝。

全部复利行为经 5 个 tool（write/search/get/link/feedback）断言，
用内存直连 server 的 mcp.Client（无 stdio 子进程）。
client 会话必须与测试同 task 开关（anyio 要求），
因此用 async helper 而非 async fixture。
只断言外部行为：排序经 search 结果断言，不经评分内部。
"""

from __future__ import annotations

import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp  # noqa: E402

from compound_memory import server as cm_server  # noqa: E402
from compound_memory.storage import MemoryStore  # noqa: E402


def call(res) -> object:
    """提取 tool 返回的 JSON 载荷。"""
    return json.loads(res.content[0].text)


# make_client 配置出的当前 store：测试经这个句柄拿 store，不摸 cm_server 私有符号
current_store: MemoryStore


@asynccontextmanager
async def make_client(root: Path, git_off: bool = False, agent_id: str | None = None):
    """配置 server store 并开内存 client；无 git 场景经 configure 注入探测 adapter。"""
    global current_store
    current_store = cm_server.configure(
        root, git=True, git_probe=(lambda: False) if git_off else None, agent_id=agent_id
    )
    async with mcp.Client(cm_server.mcp) as c:
        yield c


@pytest.fixture
def memroot(tmp_path: Path) -> Path:
    return tmp_path / "memroot"


@pytest.fixture
def memroot2(tmp_path: Path) -> Path:
    return tmp_path / "memroot2"


class TestWriteAndSearch:
    async def test_write_returns_memory_and_search_finds_it(self, memroot):
        async with make_client(memroot) as client:
            res = call(await client.call_tool("memory_write", {
                "content": "Vercel Hobby 版 Serverless 函数有 10 秒执行超时",
                "type": "episode", "source": "agent-tars",
            }))
            assert res["id"]
            assert res["ns"] == "_shared"
            assert res["type"] == "episode"
            assert res["confidence"] == pytest.approx(0.5)

            out = call(await client.call_tool("memory_search", {"query": "Vercel Serverless 超时"}))
            assert out["count"] == 1
            assert [h["id"] for h in out["hits"]] == [res["id"]]
            assert out["hits"][0]["score"] > 0

    async def test_search_rejects_empty_query(self, memroot):
        async with make_client(memroot) as client:
            out = call(await client.call_tool("memory_search", {"query": "   "}))
            assert out == {"hits": [], "count": 0}

    async def test_search_rejects_bad_ns(self, memroot):
        """拼错的 ns 是调用方错误：必须报错而非静默返回空结果——
        静默空结果会让 agent 误判"无相关记忆"（与 write 的 ns 校验同一约定）。"""
        async with make_client(memroot) as client:
            res = await client.call_tool("memory_search", {"query": "Git", "ns": "shared"})
            assert res.is_error

    async def test_search_is_namespace_scoped(self, memroot):
        async with make_client(memroot) as client:
            shared = call(await client.call_tool("memory_write", {
                "content": "共享的 Git rebase 经验", "type": "episode", "source": "agent-a",
            }))
            call(await client.call_tool("memory_write", {
                "content": "私有的 Git rebase 草稿", "type": "episode", "source": "agent-tars", "ns": "agent-tars",
            }))
            out = call(await client.call_tool("memory_search", {"query": "Git rebase"}))
            assert [h["id"] for h in out["hits"]] == [shared["id"]]

    async def test_project_scope_filters_and_write_passthrough(self, memroot):
        """ADR 0010：MCP 面的 project 参数透传——写入携带 project，检索缺省
        fail-closed（只见全局），声明项目后见 全局 ∪ 该项目。"""
        async with make_client(memroot) as client:
            glob = call(await client.call_tool("memory_write", {
                "content": "agenthub 部署流程", "type": "fact", "source": "agent-a",
            }))
            proj = call(await client.call_tool("memory_write", {
                "content": "agenthub 部署流程", "type": "fact", "source": "agent-a", "project": "agenthub",
            }))
            assert proj["project"] == "agenthub"
            out = call(await client.call_tool("memory_search", {"query": "agenthub 部署"}))
            assert [h["id"] for h in out["hits"]] == [glob["id"]]
            out = call(await client.call_tool("memory_search", {"query": "agenthub 部署", "project": "agenthub"}))
            assert {h["id"] for h in out["hits"]} == {proj["id"], glob["id"]}

    async def test_write_rejects_bad_type_and_ns(self, memroot):
        async with make_client(memroot) as client:
            res = await client.call_tool("memory_write", {"content": "x", "type": "bogus", "source": "a"})
            assert res.is_error
            res = await client.call_tool("memory_write", {"content": "x", "type": "episode", "source": "a", "ns": "shared"})
            assert res.is_error


class TestCompounding:
    async def test_feedback_raises_confidence_and_ranking(self, memroot):
        """利息①：被用过的记忆在相似查询中反超未用过的。"""
        async with make_client(memroot) as client:
            old = call(await client.call_tool("memory_write", {
                "content": "Next.js App Router 缓存策略 force-static", "type": "episode", "source": "agent-a",
            }))
            call(await client.call_tool("memory_write", {
                "content": "Next.js App Router 缓存策略 dynamic", "type": "episode", "source": "agent-b",
            }))
            call(await client.call_tool("memory_feedback", {"mem_id": old["id"], "agent": "agent-a"}))
            got = call(await client.call_tool("memory_get", {"mem_id": old["id"]}))
            assert got["uses"] == 1
            assert got["confidence"] == pytest.approx(0.6)
            out = call(await client.call_tool("memory_search", {"query": "Next.js App Router 缓存"}))
            assert out["hits"][0]["id"] == old["id"]

    async def test_cross_agent_validation_bumps_confidence(self, memroot):
        """利息④：另一个不同 agent 验证时额外 +0.15。"""
        async with make_client(memroot) as client:
            mem = call(await client.call_tool("memory_write", {
                "content": "macOS 的 sed -i 需要后备缀参数", "type": "fact", "source": "agent-a", "key": "sed-mac",
            }))
            call(await client.call_tool("memory_feedback", {"mem_id": mem["id"], "agent": "agent-b"}))
            got = call(await client.call_tool("memory_get", {"mem_id": mem["id"]}))
            assert got["confidence"] == pytest.approx(0.75)
            assert got["validated_by"] == ["agent-b"]

    async def test_link_is_bidirectional_and_get_returns_neighbors(self, memroot):
        """利息②：memory_get 会带出 links 邻居。"""
        async with make_client(memroot) as client:
            a = call(await client.call_tool("memory_write", {
                "content": "sqlite-vec 向量检索", "type": "insight", "source": "agent-a",
            }))
            b = call(await client.call_tool("memory_write", {
                "content": "ripgrep 关键词检索", "type": "insight", "source": "agent-b",
            }))
            call(await client.call_tool("memory_link", {"id_a": a["id"], "id_b": b["id"]}))
            got = call(await client.call_tool("memory_get", {"mem_id": b["id"]}))
            assert a["id"] in got["links"]
            assert a["id"] in [n["id"] for n in got["neighbors"]]

    async def test_missing_ids_are_reported_not_raised(self, memroot):
        async with make_client(memroot) as client:
            assert call(await client.call_tool("memory_get", {"mem_id": "nope"}))["found"] is False
            assert call(await client.call_tool("memory_feedback", {"mem_id": "nope", "agent": "x"}))["found"] is False
            assert call(await client.call_tool("memory_link", {"id_a": "nope", "id_b": "alsono"}))["found"] is False


class TestNeighborRecall:
    async def test_search_embeds_one_hop_neighbors(self, memroot):
        """利息②的 search 侧：命中自动带出一度邻居（带出而非提分——邻居不影响排序分）。
        单 hit + 嵌套数组场景同时验证 mcp 2.x unwrap 不破坏 {'hits': [...]} 信封。"""
        async with make_client(memroot) as client:
            a = call(await client.call_tool("memory_write", {
                "content": "sqlite-vec 向量检索", "type": "insight", "source": "agent-a",
            }))
            b = call(await client.call_tool("memory_write", {
                "content": "ripgrep 关键词检索", "type": "insight", "source": "agent-b",
            }))
            call(await client.call_tool("memory_link", {"id_a": a["id"], "id_b": b["id"]}))
            out = call(await client.call_tool("memory_search", {"query": "sqlite-vec 向量"}))
            assert out["count"] == 1 and isinstance(out["hits"], list)
            hit = out["hits"][0]
            assert hit["id"] == a["id"]
            neighbors = hit["neighbors"]
            assert [n["id"] for n in neighbors] == [b["id"]]
            assert hit["id"] not in [n["id"] for n in neighbors]  # 去环：双向 link 不带回自己
            assert neighbors[0]["type"] == "insight" and neighbors[0]["ns"] == "_shared"
            assert neighbors[0]["content"] == "ripgrep 关键词检索"

    async def test_neighbor_content_truncated_to_80_chars(self, memroot):
        async with make_client(memroot) as client:
            anchor = call(await client.call_tool("memory_write", {
                "content": "主题词锚点记忆", "type": "episode", "source": "agent-a",
            }))
            long_neighbor = call(await client.call_tool("memory_write", {
                "content": "长" * 120, "type": "episode", "source": "agent-a",
            }))
            call(await client.call_tool("memory_link", {"id_a": anchor["id"], "id_b": long_neighbor["id"]}))
            out = call(await client.call_tool("memory_search", {"query": "主题词锚点"}))
            trimmed = out["hits"][0]["neighbors"][0]["content"]
            assert len(trimmed) == 81 and trimmed.endswith("…")

    async def test_neighbor_cap_three_per_hit(self, memroot):
        async with make_client(memroot) as client:
            anchor = call(await client.call_tool("memory_write", {
                "content": "上限测试锚点", "type": "episode", "source": "agent-a",
            }))
            for i in range(4):
                side = call(await client.call_tool("memory_write", {
                    "content": f"外围节点{i}号", "type": "episode", "source": "agent-a",
                }))
                call(await client.call_tool("memory_link", {"id_a": anchor["id"], "id_b": side["id"]}))
            out = call(await client.call_tool("memory_search", {"query": "上限测试锚点"}))
            assert len(out["hits"][0]["neighbors"]) == 3

    async def test_archived_neighbor_not_recalled(self, memroot):
        """归档邻居不召回——邻居只来自活动区（decay 不经 MCP 暴露，直接走 store 归档）。"""
        async with make_client(memroot) as client:
            anchor = call(await client.call_tool("memory_write", {
                "content": "活性锚点记忆", "type": "fact", "source": "agent-a", "key": "anchor",
            }))
            stale = call(await client.call_tool("memory_write", {
                "content": "陈旧的关联邻居", "type": "episode", "source": "agent-a",
            }))
            call(await client.call_tool("memory_link", {"id_a": anchor["id"], "id_b": stale["id"]}))
            store = current_store
            store._archive(store.find(stale["id"]))  # type: ignore[arg-type]
            out = call(await client.call_tool("memory_search", {"query": "活性锚点"}))
            assert out["hits"][0]["neighbors"] == []

    async def test_include_neighbors_false_omits_key(self, memroot):
        async with make_client(memroot) as client:
            a = call(await client.call_tool("memory_write", {
                "content": "关闭邻居的锚点", "type": "episode", "source": "agent-a",
            }))
            b = call(await client.call_tool("memory_write", {
                "content": "毫不相干的外围内容", "type": "episode", "source": "agent-a",
            }))
            call(await client.call_tool("memory_link", {"id_a": a["id"], "id_b": b["id"]}))
            out = call(await client.call_tool("memory_search", {"query": "关闭邻居的锚点", "include_neighbors": False}))
            assert [h["id"] for h in out["hits"]] == [a["id"]]
            assert "neighbors" not in out["hits"][0]


class TestConflicts:
    async def test_conflicting_fact_goes_to_review_queue(self, memroot):
        async with make_client(memroot) as client:
            first = call(await client.call_tool("memory_write", {
                "content": "超时时间 10 秒", "type": "fact", "source": "agent-a", "key": "vercel-timeout",
            }))
            assert first["conflict"] is False
            second = call(await client.call_tool("memory_write", {
                "content": "超时时间 60 秒", "type": "fact", "source": "agent-b", "key": "vercel-timeout",
            }))
            assert second["conflict"] is True
            assert second["conflicts_with"] == first["id"]
            queue = current_store.review_queue()
            assert len(queue) == 1
            assert first["id"] in queue[0] and second["id"] in queue[0]

    async def test_same_content_same_key_is_not_a_conflict(self, memroot):
        async with make_client(memroot) as client:
            call(await client.call_tool("memory_write", {
                "content": "Python 3.13 是当前版本", "type": "fact", "source": "agent-a", "key": "py-ver",
            }))
            again = call(await client.call_tool("memory_write", {
                "content": "Python 3.13 是当前版本", "type": "fact", "source": "agent-b", "key": "py-ver",
            }))
            assert again["conflict"] is False


class TestNamespacePermissions:
    async def test_private_ns_rejects_foreign_writer(self, memroot):
        """spec: agent-<name> 仅 owner 可写."""
        async with make_client(memroot) as client:
            res = await client.call_tool("memory_write", {
                "content": "x", "type": "episode", "source": "agent-a", "ns": "agent-tars",
            })
            assert res.is_error

    async def test_private_ns_accepts_owner(self, memroot):
        async with make_client(memroot) as client:
            res = call(await client.call_tool("memory_write", {
                "content": "自己的草稿", "type": "episode", "source": "agent-tars", "ns": "agent-tars",
            }))
            assert res["ns"] == "agent-tars"

    async def test_private_read_requires_reader_param(self, memroot):
        """search/get 的 reader 透传：缺身份 is_error，属主身份放行."""
        async with make_client(memroot) as client:
            priv = call(await client.call_tool("memory_write", {
                "content": "私有检索草稿", "type": "fact", "source": "agent-tars", "ns": "agent-tars",
            }))
            no_reader = await client.call_tool(
                "memory_search", {"query": "私有检索", "ns": "agent-tars"}
            )
            assert no_reader.is_error
            assert (await client.call_tool("memory_get", {"mem_id": priv["id"]})).is_error
            hits = call(await client.call_tool(
                "memory_search", {"query": "私有检索", "ns": "agent-tars", "reader": "agent-tars"}
            ))
            assert [h["id"] for h in hits["hits"]] == [priv["id"]]
            got = call(await client.call_tool(
                "memory_get", {"mem_id": priv["id"], "reader": "tars"}
            ))
            assert got["found"] is True

    async def test_cross_ns_link_rejected(self, memroot):
        async with make_client(memroot) as client:
            shared = call(await client.call_tool("memory_write", {
                "content": "共享记忆", "type": "episode", "source": "agent-a",
            }))
            priv = call(await client.call_tool("memory_write", {
                "content": "私有记忆", "type": "episode", "source": "agent-tars", "ns": "agent-tars",
            }))
            res = await client.call_tool("memory_link", {"id_a": shared["id"], "id_b": priv["id"]})
            assert res.is_error

    async def test_private_link_requires_agent_param(self, memroot):
        """D1（#34）：私有 ns 的 link 仅属主——缺 agent is_error，属主短名放行。"""
        async with make_client(memroot) as client:
            a = call(await client.call_tool("memory_write", {
                "content": "私有关联甲", "type": "fact", "source": "agent-tars", "ns": "agent-tars",
            }))
            b = call(await client.call_tool("memory_write", {
                "content": "私有关联乙", "type": "fact", "source": "agent-tars", "ns": "agent-tars",
            }))
            res = await client.call_tool("memory_link", {"id_a": a["id"], "id_b": b["id"]})
            assert res.is_error
            forged = await client.call_tool(
                "memory_link", {"id_a": a["id"], "id_b": b["id"], "agent": "agent-a"}
            )
            assert forged.is_error
            ok = call(await client.call_tool(
                "memory_link", {"id_a": a["id"], "id_b": b["id"], "agent": "tars"}
            ))
            assert ok["found"] is True

    async def test_private_feedback_rejects_foreign_agent(self, memroot):
        async with make_client(memroot) as client:
            priv = call(await client.call_tool("memory_write", {
                "content": "私有反馈目标", "type": "fact", "source": "agent-tars", "ns": "agent-tars",
            }))
            res = await client.call_tool("memory_feedback", {"mem_id": priv["id"], "agent": "agent-a"})
            assert res.is_error
            ok = call(await client.call_tool("memory_feedback", {"mem_id": priv["id"], "agent": "tars"}))
            assert ok["uses"] == 1


class TestSingleCopySerialization:
    """structured_output=False：载荷只走 text 一份，无 structuredContent 双份下发。"""

    async def test_no_structured_content_duplication(self, memroot):
        async with make_client(memroot) as client:
            res = await client.call_tool("memory_search", {"query": "anything"})
            assert res.structured_content is None
            assert len(res.content) == 1
            assert res.content[0].type == "text"


class TestAttestationWiring:
    """进程身份注入（configure 的 agent_id，生产来自 COMPOUND_MEMORY_AGENT_ID）经 tool 缝生效。"""

    async def test_forged_identity_rejected(self, memroot):
        async with make_client(memroot, agent_id="agent-zcode") as client:
            res = await client.call_tool("memory_write", {
                "content": "x", "type": "fact", "source": "agent-a",
            })
            assert res.is_error
            ok = call(await client.call_tool("memory_write", {
                "content": "x", "type": "fact", "source": "agent-zcode",
            }))
            assert ok["source"] == "agent-zcode"

    async def test_reader_autofilled_from_process_identity(self, memroot):
        async with make_client(memroot, agent_id="agent-zcode") as client:
            priv = call(await client.call_tool("memory_write", {
                "content": "私有自动补 reader", "type": "fact", "source": "agent-zcode", "ns": "agent-zcode",
            }))
            hits = call(await client.call_tool("memory_search", {"query": "私有自动补", "ns": "agent-zcode"}))
            assert [h["id"] for h in hits["hits"]] == [priv["id"]]


class TestWithoutGit:
    async def test_tools_work_without_git(self, memroot2):
        async with make_client(memroot2, git_off=True) as client:
            res = call(await client.call_tool("memory_write", {
                "content": "无 git 环境下也能写", "type": "episode", "source": "agent-a",
            }))
            out = call(await client.call_tool("memory_search", {"query": "无 git"}))
            assert [h["id"] for h in out["hits"]] == [res["id"]]
