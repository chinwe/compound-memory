"""CLI：不经 MCP 直接访问同一 store（脚本、定时蒸馏、衰减扫描等运维面）。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .storage import MEMORY_TYPES, MemoryStore


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _open_store(args: argparse.Namespace) -> MemoryStore:
    return MemoryStore(Path(args.root))


def cmd_init(args: argparse.Namespace) -> None:
    store = _open_store(args)
    _emit({"ok": True, "root": str(store.root)})


def cmd_write(args: argparse.Namespace) -> None:
    _emit(_open_store(args).write(content=args.content, type=args.type, source=args.source, ns=args.ns, key=args.key))


def cmd_search(args: argparse.Namespace) -> None:
    _emit(_open_store(args).search(query=args.query, ns=args.ns, top_k=args.top_k))


def cmd_get(args: argparse.Namespace) -> None:
    _emit(_open_store(args).get(args.id))


def cmd_link(args: argparse.Namespace) -> None:
    _emit(_open_store(args).link(args.a, args.b))


def cmd_feedback(args: argparse.Namespace) -> None:
    _emit(_open_store(args).feedback(args.id, args.agent))


def cmd_decay(args: argparse.Namespace) -> None:
    now = dt.date.fromisoformat(args.now) if args.now else None
    _emit({"archived": _open_store(args).decay_sweep(now=now)})


def cmd_revive(args: argparse.Namespace) -> None:
    _emit(_open_store(args).revive(args.id))


def cmd_stats(args: argparse.Namespace) -> None:
    _emit(_open_store(args).stats())


def cmd_rebuild_index(args: argparse.Namespace) -> None:
    _emit(_open_store(args).rebuild_index())


def cmd_review_queue(args: argparse.Namespace) -> None:
    _emit(_open_store(args).review_queue())


def cmd_distill_plan(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).distill_plan(
            window_days=args.window,
            min_uses=args.min_uses,
            min_confidence=args.min_confidence,
            ns=args.ns,
        )
    )


def cmd_distill_apply(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).distill_apply(
            content=args.content,
            type=args.type,
            source=args.source,
            source_ids=[s.strip() for s in args.sources.split(",") if s.strip()],
            ns=args.ns,
            key=args.key,
            confidence=args.confidence,
        )
    )


def cmd_git_log(args: argparse.Namespace) -> None:
    _emit(_open_store(args).git_log(limit=args.limit))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="compound-memory", description="local multi-agent shared memory")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--root", default=None, help="store root (default: $COMPOUND_MEMORY_ROOT or ~/.agents/memory)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init"); p.set_defaults(func=cmd_init)

    p = sub.add_parser("write")
    p.add_argument("content"); p.add_argument("type", choices=MEMORY_TYPES)
    p.add_argument("source"); p.add_argument("--ns", default="_shared"); p.add_argument("--key", default=None)
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("search")
    p.add_argument("query"); p.add_argument("--ns", default="_shared"); p.add_argument("--top-k", type=int, default=5)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("get"); p.add_argument("id"); p.set_defaults(func=cmd_get)
    p = sub.add_parser("link"); p.add_argument("a"); p.add_argument("b"); p.set_defaults(func=cmd_link)
    p = sub.add_parser("feedback"); p.add_argument("id"); p.add_argument("agent"); p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("decay"); p.add_argument("--now", default=None, help="ISO date override (testing)")
    p.set_defaults(func=cmd_decay)

    p = sub.add_parser("revive"); p.add_argument("id"); p.set_defaults(func=cmd_revive)
    p = sub.add_parser(
        "distill-plan",
        help="scan distillation candidates and print a signal-annotated list",
        description="Scan distillation candidates (deterministic half of distillation). Signals per candidate: "
        "merge_with (same ns/type/key, strong), possible_dup_of (BM25 normalized_similarity >= 0.5, weak), "
        "promotion_candidate (episode uses >= 5). Judgment (merge/summarize) stays with the calling agent.",
    )
    p.add_argument("--window", type=int, default=30, help="recency window in days (last_used first, created fallback)")
    p.add_argument("--min-uses", type=int, default=1, help="activity gate: uses >= this")
    p.add_argument("--min-confidence", type=float, default=0.5, help="activity gate: confidence >= this")
    p.add_argument("--ns", default="_shared")
    p.set_defaults(func=cmd_distill_plan)
    p = sub.add_parser("distill-apply")
    p.add_argument("content"); p.add_argument("type", choices=MEMORY_TYPES); p.add_argument("source")
    p.add_argument("--sources", required=True, help="comma-separated source memory ids")
    p.add_argument("--ns", default="_shared"); p.add_argument("--key", default=None)
    p.add_argument("--confidence", type=float, default=None)
    p.set_defaults(func=cmd_distill_apply)
    p = sub.add_parser("stats"); p.set_defaults(func=cmd_stats)
    p = sub.add_parser("rebuild-index"); p.set_defaults(func=cmd_rebuild_index)
    p = sub.add_parser("review-queue"); p.set_defaults(func=cmd_review_queue)
    p = sub.add_parser("git-log"); p.add_argument("--limit", type=int, default=5); p.set_defaults(func=cmd_git_log)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.root is None:
        import os

        args.root = os.environ.get("COMPOUND_MEMORY_ROOT", str(Path.home() / ".agents" / "memory"))
    try:
        args.func(args)
        return 0
    except (ValueError, PermissionError) as exc:
        # store 接口的调用方错误统一在这里翻译成 JSON（MCP 侧由框架转 is_error）
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
