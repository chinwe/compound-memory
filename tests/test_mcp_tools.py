"""Tests at the MCP tool boundary — the single seam agreed in the spec.

All compounding behaviour is asserted via the 5 tools (write/search/get/link/feedback),
using an in-memory client connected directly to the server (no stdio subprocess).
The client session must open/close inside the same task as the test (anyio requirement),
so we use a helper instead of an async fixture.
External behaviour only: ordering asserted via search results, never via scoring internals.
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


def call(res) -> object:
    """Extract the JSON payload a tool returned."""
    return json.loads(res.content[0].text)


@asynccontextmanager
async def make_client(root: Path, patch_git_off=False):
    if patch_git_off:
        import shutil as _shutil

        real_which = _shutil.which
        _shutil.which = lambda name: None if name == "git" else real_which(name)
    cm_server.configure(root, git=True)
    try:
        async with mcp.Client(cm_server.mcp) as c:
            yield c
    finally:
        if patch_git_off:
            _shutil.which = real_which  # type: ignore[possibly-undefined]


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

    async def test_write_rejects_bad_type_and_ns(self, memroot):
        async with make_client(memroot) as client:
            res = await client.call_tool("memory_write", {"content": "x", "type": "bogus", "source": "a"})
            assert res.is_error
            res = await client.call_tool("memory_write", {"content": "x", "type": "episode", "source": "a", "ns": "shared"})
            assert res.is_error


class TestCompounding:
    async def test_feedback_raises_confidence_and_ranking(self, memroot):
        """利息①: used memory outranks a similar unused one."""
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
        """利息④: a second, different agent validating gives +0.15 extra."""
        async with make_client(memroot) as client:
            mem = call(await client.call_tool("memory_write", {
                "content": "macOS 的 sed -i 需要后备缀参数", "type": "fact", "source": "agent-a", "key": "sed-mac",
            }))
            call(await client.call_tool("memory_feedback", {"mem_id": mem["id"], "agent": "agent-b"}))
            got = call(await client.call_tool("memory_get", {"mem_id": mem["id"]}))
            assert got["confidence"] == pytest.approx(0.75)
            assert got["validated_by"] == ["agent-b"]

    async def test_link_is_bidirectional_and_get_returns_neighbors(self, memroot):
        """利息②: neighbors come back with memory_get."""
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
            queue = cm_server._store_or_configure().review_queue_path.read_text(encoding="utf-8")
            assert first["id"] in queue and second["id"] in queue

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
        """spec: agent-<name> 仅 owner 读写."""
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


class TestWithoutGit:
    async def test_tools_work_without_git(self, memroot2):
        async with make_client(memroot2, patch_git_off=True) as client:
            res = call(await client.call_tool("memory_write", {
                "content": "无 git 环境下也能写", "type": "episode", "source": "agent-a",
            }))
            out = call(await client.call_tool("memory_search", {"query": "无 git"}))
            assert [h["id"] for h in out["hits"]] == [res["id"]]
