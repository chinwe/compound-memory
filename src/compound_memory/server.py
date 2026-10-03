"""MCP server：所有 Agent 的唯一读写边界。

恰好 5 个 tool：memory_write / memory_search / memory_get / memory_link / memory_feedback。
memory_feedback 是一等公民——复利闭环依赖它。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer

from .embedding import auto_encoder
from .storage import MEMORY_TYPES, MemoryStore, default_root

mcp = MCPServer("compound-memory")

_store: MemoryStore | None = None


def configure(
    root: Path | str | None = None,
    git: bool = True,
    git_probe: Callable[[], bool] | None = None,
    embedder: Callable[[list[str]], list[list[float]]] | None = None,
    agent_id: str | None = None,
) -> MemoryStore:
    global _store
    _store = MemoryStore(
        Path(root) if root is not None else default_root(),
        git=git,
        git_probe=git_probe,
        embedder=embedder,
        agent_id=agent_id,
    )
    return _store


def _store_or_configure() -> MemoryStore:
    if _store is None:
        configure()
    assert _store is not None
    return _store


@mcp.tool()
def memory_write(
    content: str,
    type: str,
    source: str,
    ns: str = "_shared",
    key: str | None = None,
    links: list[str] | None = None,
) -> dict[str, Any]:
    """Write a memory. type: episode|fact|insight|skill; source: writing agent id; ns: '_shared' or 'agent-<name>'. key: stable id for fact/insight (enables conflict review). Write only stable facts (preferences, conventions, environment constraints, pitfalls), not session-temporary details; prefer reusing an existing key over a new entry. Returns the stored memory; `conflict: true` means a different version with the same key exists and a review entry was queued."""
    return _store_or_configure().write(content=content, type=type, source=source, ns=ns, key=key, links=links)


@mcp.tool()
def memory_search(
    query: str,
    ns: str = "_shared",
    top_k: int = 5,
    include_neighbors: bool = True,
    reader: str | None = None,
) -> dict[str, Any]:
    """Search memories. Fuses lexical (BM25) and, when the vec extra + model are installed, vector (BGE) recall via RRF; otherwise falls back to lexical only. Confidence/recency/type act only as a small tie-break. Default namespace is _shared. Each hit embeds up to 3 trimmed one-hop neighbors (active only) unless include_neighbors=False. reader: your own source agent id — REQUIRED when ns is 'agent-<name>' (private namespace, readable only by its owner host); ignored for _shared. Returns {'hits': [...]} sorted by score. Compounding rule: after actually adopting a hit, call memory_feedback (agent = your source id) — skipped feedbacks leave the store static."""
    hits = _store_or_configure().search(
        query=query, ns=ns, top_k=top_k, include_neighbors=include_neighbors, reader=reader
    )
    return {"hits": hits, "count": len(hits)}


@mcp.tool()
def memory_get(mem_id: str, include_neighbors: bool = True, reader: str | None = None) -> dict[str, Any]:
    """Fetch a memory by id; one-hop link neighbors are included by default. reader: your own source agent id — required when the memory lives in a private 'agent-<name>' namespace (readable only by its owner host). After adopting it, call memory_feedback (agent = your source id)."""
    return _store_or_configure().get(mem_id, include_neighbors=include_neighbors, reader=reader)


@mcp.tool()
def memory_link(id_a: str, id_b: str) -> dict[str, Any]:
    """Create a bidirectional link between two memories (compounding source #2: association). Both memories must live in the same namespace; cross-namespace links are rejected."""
    return _store_or_configure().link(id_a, id_b)


@mcp.tool()
def memory_feedback(mem_id: str, agent: str) -> dict[str, Any]:
    """Report that a memory was actually used. Increments uses, raises confidence (+0.1; extra +0.15 when a different agent validates). agent must be your own source agent id. Memories in a private 'agent-<name>' namespace accept feedback only from the owner (agent = 'agent-<name>' or '<name>'). Archiving is reversed on feedback. MUST be called after a memory is adopted — this closes the compounding loop."""
    return _store_or_configure().feedback(mem_id, agent)


def main() -> None:
    if _store is None:
        # 生产入口自动挂向量路（vec extra + 模型就绪才生效，否则静默降级纯词面）；
        # 宿主经 COMPOUND_MEMORY_AGENT_ID 注入进程身份，未设置则保持自报身份模式
        configure(embedder=auto_encoder(), agent_id=os.environ.get("COMPOUND_MEMORY_AGENT_ID") or None)
    mcp.run()


if __name__ == "__main__":
    main()
