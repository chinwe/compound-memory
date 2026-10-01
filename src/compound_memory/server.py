"""MCP server: the single read/write boundary for all agents.

Exactly five tools: memory_write / memory_search / memory_get / memory_link / memory_feedback.
memory_feedback is a first-class citizen — the compounding loop depends on it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from .storage import MEMORY_TYPES, MemoryStore

mcp = MCPServer("compound-memory")

_store: MemoryStore | None = None


def default_root() -> Path:
    env = os.environ.get("COMPOUND_MEMORY_ROOT")
    return Path(env) if env else Path.home() / ".agents" / "memory"


def configure(root: Path | str | None = None, git: bool = True) -> MemoryStore:
    global _store
    _store = MemoryStore(Path(root) if root is not None else default_root(), git=git)
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
    """Write a memory. type: episode|fact|insight|skill; source: writing agent id; ns: '_shared' or 'agent-<name>'. key: stable id for fact/insight (enables conflict review). Returns the stored memory; `conflict: true` means a different version with the same key exists and a review entry was queued."""
    return _store_or_configure().write(content=content, type=type, source=source, ns=ns, key=key, links=links)


@mcp.tool()
def memory_search(query: str, ns: str = "_shared", top_k: int = 5) -> dict[str, Any]:
    """Hybrid search (lexical similarity + confidence + recency + type weight). Default namespace is _shared. Returns {'hits': [...]} sorted by score."""
    hits = _store_or_configure().search(query=query, ns=ns, top_k=top_k)
    return {"hits": hits, "count": len(hits)}


@mcp.tool()
def memory_get(mem_id: str, include_neighbors: bool = True) -> dict[str, Any]:
    """Fetch a memory by id; one-hop link neighbors are included by default."""
    return _store_or_configure().get(mem_id, include_neighbors=include_neighbors)


@mcp.tool()
def memory_link(id_a: str, id_b: str) -> dict[str, Any]:
    """Create a bidirectional link between two memories (compounding source #2: association)."""
    return _store_or_configure().link(id_a, id_b)


@mcp.tool()
def memory_feedback(mem_id: str, agent: str) -> dict[str, Any]:
    """Report that a memory was actually used. Increments uses, raises confidence (+0.1; extra +0.15 when a different agent validates). Archiving is reversed on feedback. MUST be called after a memory is adopted."""
    return _store_or_configure().feedback(mem_id, agent)


def main() -> None:
    _store_or_configure()
    mcp.run()


if __name__ == "__main__":
    main()
