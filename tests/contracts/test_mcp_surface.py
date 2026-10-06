"""MCP 层契约（#25 决议第二优先级）：只钉 tool 名、返回形状、异常→is_error 翻译。

语义不重测（store 层契约已钉）；mcp.Client 会话与测试同 task 内开关
（anyio cancel scope 约束，见 AGENTS.md 沙箱坑）。
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import mcp  # noqa: E402

from compound_memory import server as cm_server  # noqa: E402

EXPECTED_TOOLS = {"memory_write", "memory_search", "memory_get", "memory_link", "memory_feedback"}


def call(res) -> object:
    """提取 tool 返回的 JSON 载荷。"""
    return json.loads(res.content[0].text)


@asynccontextmanager
async def make_client(root: Path):
    """配置 server store 并开内存 client（同 task 内开关，勿改为 fixture）。"""
    cm_server.configure(root, git=True)
    async with mcp.Client(cm_server.mcp) as client:
        yield client


@pytest.fixture
def memroot(tmp_path: Path) -> Path:
    return tmp_path / "memroot"


class TestToolSurface:
    async def test_exactly_five_tools_exposed(self, memroot: Path):
        """恰好 5 个 tool（唯一读写边界，勿增删）——tool 名是宿主集成契约。"""
        async with make_client(memroot) as client:
            listed = await client.list_tools()
            assert {t.name for t in listed.tools} == EXPECTED_TOOLS


class TestReturnShapes:
    async def test_memory_search_wraps_hits_envelope(self, memroot: Path):
        """返回形状红线：{"hits": [...], "count": n} 包装只在 MCP 层（CLI 是裸数组）。"""
        async with make_client(memroot) as client:
            written = call(await client.call_tool("memory_write", {
                "content": "contract mcp shape marker", "type": "fact", "source": "agent-a",
            }))
            out = call(await client.call_tool("memory_search", {"query": "mcp shape marker"}))
            assert set(out) == {"hits", "count"}
            assert out["count"] == 1
            assert [h["id"] for h in out["hits"]] == [written["id"]]

    async def test_memory_write_returns_stored_memory(self, memroot: Path):
        async with make_client(memroot) as client:
            res = call(await client.call_tool("memory_write", {
                "content": "contract mcp write shape", "type": "episode", "source": "agent-a",
            }))
            assert res["id"] and res["ns"] == "_shared"
            assert res["type"] == "episode"
            assert res["conflict"] is False

    async def test_by_id_tools_return_found_envelopes(self, memroot: Path):
        """按 id 动词信封：目标不存在 ⇒ found:False（不是异常、不是第三种键名）。"""
        async with make_client(memroot) as client:
            assert call(await client.call_tool("memory_get", {"mem_id": "nope"})) == {"found": False}
            assert call(await client.call_tool("memory_feedback", {"mem_id": "nope", "agent": "x"})) == {
                "found": False,
            }
            assert call(await client.call_tool("memory_link", {"id_a": "nope", "id_b": "alsono"})) == {
                "found": False, "missing": ["nope", "alsono"],
            }

    async def test_single_copy_serialization(self, memroot: Path):
        """structured_output=False：载荷只走 text 一份，无 structuredContent 双份下发。"""
        async with make_client(memroot) as client:
            res = await client.call_tool("memory_search", {"query": "anything"})
            assert res.structured_content is None
            assert len(res.content) == 1 and res.content[0].type == "text"


class TestErrorTranslation:
    """store 抛出的调用方错误（ValueError/PermissionError）由 MCP 框架翻译成
    is_error 结果——语义断言在 store 层，这里只钉「异常必被翻译、不裸抛」。"""

    async def test_value_error_becomes_is_error(self, memroot: Path):
        async with make_client(memroot) as client:
            res = await client.call_tool("memory_search", {"query": "x", "ns": "shared"})
            assert res.is_error

    async def test_permission_error_becomes_is_error(self, memroot: Path):
        async with make_client(memroot) as client:
            res = await client.call_tool("memory_write", {
                "content": "x", "type": "episode", "source": "agent-workbuddy", "ns": "agent-tars",
            })
            assert res.is_error

    async def test_missing_target_is_not_an_error(self, memroot: Path):
        """信封类「错误」不是异常：missing id 走正常返回（is_error 为假）。"""
        async with make_client(memroot) as client:
            res = await client.call_tool("memory_get", {"mem_id": "nope"})
            assert not res.is_error
