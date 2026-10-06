"""MCP server：所有 Agent 的唯一读写边界。

恰好 5 个 tool：memory_write / memory_search / memory_get / memory_link / memory_feedback。
memory_feedback 是一等公民——复利闭环依赖它。

所有 tool 声明 structured_output=False（单份序列化）：mcp 2.x 会从 `dict[str, Any]`
注解推断 outputSchema，结构化载荷与文本回退同时下发双份，撑大宿主上下文；
关闭后只走 text 一份 JSON，形状不变。
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


@mcp.tool(structured_output=False)
def memory_write(
    content: str,
    type: str,
    source: str,
    ns: str = "_shared",
    key: str | None = None,
    links: list[str] | None = None,
    valid_from: str | None = None,
    valid_until: str | None = None,
) -> dict[str, Any]:
    """Write a memory. type: episode|fact|insight|skill; source: writing agent id; ns: '_shared' or 'agent-<name>'. key: stable id for fact/insight (enables conflict review). Write only stable facts (preferences, conventions, environment constraints, pitfalls), not session-temporary details; volatile status notes (in-progress work, remaining todos) either carry valid_until or stay out — a stale status memory is worse than none; prefer reusing an existing key over a new entry. valid_from/valid_until: optional ISO dates (YYYY-MM-DD) marking the fact's validity window — once valid_until has passed, the memory is excluded from search results but still readable via memory_get. Returns the stored memory; `conflict: true` means a different version with the same key exists and a review entry was queued."""
    return _store_or_configure().write(
        content=content,
        type=type,
        source=source,
        ns=ns,
        key=key,
        links=links,
        valid_from=valid_from,
        valid_until=valid_until,
    )


@mcp.tool(structured_output=False)
def memory_search(
    query: str,
    ns: str | None = None,
    top_k: int = 5,
    include_neighbors: bool = True,
    reader: str | None = None,
) -> dict[str, Any]:
    """Search memories. Fuses lexical (BM25) and, when the vec extra + model are installed, vector (BGE) recall via RRF; otherwise falls back to lexical only. Confidence/recency/type act only as a small tie-break. Default scope is _shared PLUS your own private 'agent-<name>' namespace (when your identity is known via attested process id or explicit reader) — private hits surface automatically, no extra query needed. Pass ns explicitly ('_shared' or 'agent-<name>') to search a single namespace. Each hit embeds up to 3 trimmed one-hop neighbors (active only) unless include_neighbors=False. reader: your own source agent id — REQUIRED when ns is 'agent-<name>' (private namespace, readable only by its owner host). Returns {'hits': [...]} sorted by score. Compounding rule: after actually adopting a hit, call memory_feedback (agent = your source id) — skipped feedbacks leave the store static."""
    hits = _store_or_configure().search(
        query=query, ns=ns, top_k=top_k, include_neighbors=include_neighbors, reader=reader
    )
    return {"hits": hits, "count": len(hits)}


@mcp.tool(structured_output=False)
def memory_get(mem_id: str, include_neighbors: bool = True, reader: str | None = None) -> dict[str, Any]:
    """Fetch a memory by id; one-hop link neighbors are included by default. reader: your own source agent id — required when the memory lives in a private 'agent-<name>' namespace (readable only by its owner host). After adopting it, call memory_feedback (agent = your source id)."""
    return _store_or_configure().get(mem_id, include_neighbors=include_neighbors, reader=reader)


@mcp.tool(structured_output=False)
def memory_link(id_a: str, id_b: str, agent: str | None = None) -> dict[str, Any]:
    """Create a bidirectional link between two memories (compounding source #2: association). Both memories must live in the same namespace; cross-namespace links are rejected. Memories in a private 'agent-<name>' namespace accept links only from the owner (agent = your own source agent id)."""
    return _store_or_configure().link(id_a, id_b, agent=agent)


@mcp.tool(structured_output=False)
def memory_feedback(mem_id: str, agent: str, outcome: str = "success") -> dict[str, Any]:
    """Report feedback on a memory with an outcome (closes the compounding loop — call after actually adopting a memory). outcome: 'success' (default, the memory worked), 'failure' (it misled you — confidence drops 0.2, floor 0.05), 'contradiction' (you dispute it — confidence frozen and a review entry is queued pending adjudication), 'obsolete' (it is superseded — archived immediately), 'unknown' (records the event only). Anything else is rejected. Success raises confidence (+0.1; extra +0.15 when a different agent validates for the first time). agent must be your own source agent id. Memories in a private 'agent-<name>' namespace accept feedback only from the owner (agent = 'agent-<name>' or '<name>'). Archiving is reversed on feedback (except outcome=obsolete, which archives instead)."""
    return _store_or_configure().feedback(mem_id, agent, outcome)


def main() -> None:
    if _store is None:
        # 生产入口自动挂向量路（vec extra + 模型就绪才生效，否则静默降级纯词面）；
        # 宿主经 COMPOUND_MEMORY_AGENT_ID 注入进程身份，未设置则保持自报身份模式
        configure(embedder=auto_encoder(), agent_id=os.environ.get("COMPOUND_MEMORY_AGENT_ID") or None)
    mcp.run()


if __name__ == "__main__":
    main()
