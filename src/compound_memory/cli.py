"""CLI：不经 MCP 直接访问同一 store（脚本、定时蒸馏、衰减扫描等运维面）。"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .embedding import auto_encoder
from .extraction import extract, extract_dir
from .storage import DISTILL_DUP_SIM_THRESHOLD, MEMORY_TYPES, MemoryStore, PROMOTION_USES_THRESHOLD, default_root


def _emit(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _open_store(args: argparse.Namespace) -> MemoryStore:
    # CLI 与 server 同权：vec extra + 模型就绪即启用向量路，否则自动降级纯词面；
    # 环境注入的进程身份（若有）同样生效——未设置时保持自报身份模式
    return MemoryStore(
        Path(args.root),
        embedder=auto_encoder(),
        agent_id=os.environ.get("COMPOUND_MEMORY_AGENT_ID") or None,
    )


def cmd_init(args: argparse.Namespace) -> None:
    store = _open_store(args)
    _emit({"ok": True, "root": str(store.root)})


def cmd_write(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).write(
            content=args.content,
            type=args.type,
            source=args.source,
            ns=args.ns,
            key=args.key,
            valid_from=args.valid_from,
            valid_until=args.valid_until,
        )
    )


def cmd_search(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).search(
            query=args.query,
            ns=args.ns,
            top_k=args.top_k,
            include_neighbors=args.include_neighbors,
            reader=args.reader,
        )
    )


def cmd_get(args: argparse.Namespace) -> None:
    _emit(_open_store(args).get(args.id, reader=args.reader))


def cmd_link(args: argparse.Namespace) -> None:
    _emit(_open_store(args).link(args.a, args.b))


def cmd_feedback(args: argparse.Namespace) -> None:
    _emit(_open_store(args).feedback(args.id, args.agent))


def cmd_decay(args: argparse.Namespace) -> None:
    # --now 经固定 clock 的 store 注入——时间接缝只有 clock 一条（store 不另设 now= 参数）
    if args.now:
        store = MemoryStore(Path(args.root), clock=lambda: dt.date.fromisoformat(args.now))
    else:
        store = _open_store(args)
    _emit({"archived": store.decay_sweep()})


def cmd_revive(args: argparse.Namespace) -> None:
    _emit(_open_store(args).revive(args.id, reader=args.reader))


def cmd_stats(args: argparse.Namespace) -> None:
    _emit(_open_store(args).stats())


def cmd_rebuild_index(args: argparse.Namespace) -> None:
    _emit(_open_store(args).rebuild_index())


def cmd_review_queue(args: argparse.Namespace) -> None:
    _emit(_open_store(args).review_queue())


def cmd_review_resolve(args: argparse.Namespace) -> None:
    _emit(_open_store(args).review_resolve(ids=args.ids, all=args.all))


def cmd_distill_plan(args: argparse.Namespace) -> None:
    _emit(
        _open_store(args).distill_plan(
            window_days=args.window,
            min_uses=args.min_uses,
            min_confidence=args.min_confidence,
            ns=args.ns,
            reader=args.reader,
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


def cmd_extract(args: argparse.Namespace) -> None:
    target = Path(args.transcript)
    if target.is_dir():
        _emit(extract_dir(target, _open_store(args)))
    else:
        _emit(extract(target, _open_store(args)))


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
    p.add_argument("--valid-from", default=None, help="ISO date: fact valid from (annotation)")
    p.add_argument("--valid-until", default=None,
                   help="ISO date: fact expires after this day (excluded from search, still readable via get)")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("search")
    p.add_argument("query")
    p.add_argument("--ns", default=None,
                   help="scope: '_shared', 'agent-<name>', or omit for _shared + your own private ns")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.add_argument("--no-neighbors", dest="include_neighbors", action="store_false",
                   help="omit embedded one-hop neighbors from hits")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("get")
    p.add_argument("id")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.set_defaults(func=cmd_get)
    p = sub.add_parser("link"); p.add_argument("a"); p.add_argument("b"); p.set_defaults(func=cmd_link)
    p = sub.add_parser("feedback"); p.add_argument("id"); p.add_argument("agent"); p.set_defaults(func=cmd_feedback)

    p = sub.add_parser("decay"); p.add_argument("--now", default=None, help="ISO date override (testing)")
    p.set_defaults(func=cmd_decay)

    p = sub.add_parser("revive")
    p.add_argument("id")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
    p.set_defaults(func=cmd_revive)
    p = sub.add_parser(
        "distill-plan",
        help="scan distillation candidates and print a signal-annotated list",
        description="Scan distillation candidates (deterministic half of distillation). Signals per candidate: "
        f"merge_with (same ns/type/key, strong), possible_dup_of (BM25 normalized_similarity >= {DISTILL_DUP_SIM_THRESHOLD}, weak), "
        f"promotion_candidate (episode uses >= {PROMOTION_USES_THRESHOLD}). Judgment (merge/summarize) stays with the calling agent.",
    )
    p.add_argument("--window", type=int, default=30, help="recency window in days (last_used first, created fallback)")
    p.add_argument("--min-uses", type=int, default=1, help="activity gate: uses >= this")
    p.add_argument("--min-confidence", type=float, default=0.5, help="activity gate: confidence >= this")
    p.add_argument("--ns", default="_shared")
    p.add_argument("--reader", default=None, help="caller identity, required for private agent-* namespaces")
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
    p = sub.add_parser(
        "review-resolve",
        help="mark review-queue conflicts as resolved",
        description="Clear review-queue entries after the caller has judged the conflict "
        "(old/new trade-off stays with the calling agent or human). Pass memory ids to clear "
        "matching rows (either the old or new id of a row counts), or --all to clear the queue. "
        "Unknown ids are rejected atomically; the cleanup is auto-committed.",
    )
    p.add_argument("ids", nargs="*", metavar="ID")
    p.add_argument("--all", action="store_true", help="clear the whole queue")
    p.set_defaults(func=cmd_review_resolve)
    p = sub.add_parser("git-log"); p.add_argument("--limit", type=int, default=5); p.set_defaults(func=cmd_git_log)
    p = sub.add_parser(
        "extract",
        help="scan a session transcript for memory candidates (deterministic pass, no LLM)",
        description="Deterministic candidate extraction from a session transcript. Transcript shapes are "
        "auto-detected by content (not filename): WorkBuddy session log jsonl, the ZCode session database "
        "(~/.zcode/cli/db/db.sqlite, full history), Claude Code session log jsonl, and DeepSeek Harness "
        "zstd-compressed session files. Pass a directory to batch-scan every supported session file under it "
        "(<project>/<session>.jsonl layouts and <project>/<session>/session.jsonl.zstd; subagents/ skipped — "
        "their role:user is the team-lead agent's task brief, not the human's own statement). "
        "ZCode rollout/model-io snapshots and WorkBuddy traces/ are deliberately unsupported — they keep only "
        "the most recent / first turns, so accepting them would look like a scan while silently dropping most "
        "of the history. "
        "Pattern matching only (statement -> fact, pitfall -> insight); the manifest lands in "
        "<root>/extract/last-candidates.json. Writing stays with the agent: confirm each candidate "
        "via memory_write (same-key conflicts still enter the review queue).",
    )
    p.add_argument(
        "transcript",
        help="path to a session log jsonl (WorkBuddy / Claude Code), the ZCode session database (.sqlite), "
        "a DeepSeek Harness session file (.jsonl.zstd), or a directory of session logs",
    )
    p.set_defaults(func=cmd_extract)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.root is None:
        args.root = default_root()
    try:
        args.func(args)
        return 0
    except (ValueError, PermissionError) as exc:
        # store 接口的调用方错误统一在这里翻译成 JSON（MCP 侧由框架转 is_error）
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
