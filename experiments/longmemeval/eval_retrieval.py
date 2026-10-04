#!/usr/bin/env python3
"""LongMemEval 检索层评测（零 LLM）：recall@k / MRR 对齐官方 oracle 证据会话。

每题在独占私有 ns 内检索（与灌库协议对齐），query = question 原文；
一次 top-20 检索后切 top-5/10/20 报 recall，MRR 用全 20 位排名。
分 question_type 输出 breakdown（temporal-reasoning / knowledge-update 是
"新旧事实并存"模式的试金石）。生成与判分（端到端 QA）不在本脚本——见 README。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compound_memory.storage import MemoryStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="zero-LLM retrieval eval on LongMemEval")
    ap.add_argument("--data", type=Path, default=Path(__file__).parent / "data" / "longmemeval_s_cleaned.json")
    ap.add_argument("--root", type=Path, required=True, help="store root used by ingest.py")
    ap.add_argument("--mapping", type=Path, required=True, help="mem_id -> session_id mapping from ingest.py")
    ap.add_argument("--limit", type=int, default=None, help="只评前 N 题（与灌库的 --limit 对齐）")
    ap.add_argument("--vector", action="store_true", help="启用向量路（需 vec extra + BGE 模型）")
    ap.add_argument("--topk", type=int, default=20)
    ap.add_argument("--out", type=Path, default=None, help="结果落盘路径（缺省 runs/ 自动命名）")
    args = ap.parse_args()

    with open(args.data, encoding="utf-8") as fh:
        questions = json.load(fh)
    if args.limit:
        questions = questions[: args.limit]
    with open(args.mapping, encoding="utf-8") as fh:
        mapping: dict[str, str] = json.load(fh)

    embedder = None
    if args.vector:
        from compound_memory.embedding import auto_encoder

        embedder = auto_encoder()
        if embedder is None:
            raise SystemExit("--vector requires the vec extra + local BGE model")

    store = MemoryStore(args.root, git=False, embedder=embedder)
    per_type: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "r5": 0, "r10": 0, "r20": 0, "mrr": 0.0})
    t0 = time.time()
    search_secs = 0.0
    for q in questions:
        ns = f"agent-lme-{q['question_id']}"
        t1 = time.time()
        hits = store.search(
            q["question"], ns=ns, reader=ns.removeprefix("agent-"),
            top_k=args.topk, include_neighbors=False,
        )
        search_secs += time.time() - t1
        got = [mapping[h["id"]] for h in hits]
        oracle = set(q["answer_session_ids"])
        first = next((i for i, s in enumerate(got, 1) if s in oracle), None)
        bucket = per_type[q["question_type"]]
        bucket["n"] += 1
        bucket["r5"] += int(first is not None and first <= 5)
        bucket["r10"] += int(first is not None and first <= 10)
        bucket["r20"] += int(first is not None)
        bucket["mrr"] += (1.0 / first) if first else 0.0

    total = {k: sum(b[k] for b in per_type.values()) for k in ("n", "r5", "r10", "r20", "mrr")}
    result = {
        "questions": len(questions),
        "vector": args.vector,
        "topk": args.topk,
        "wall_secs": round(time.time() - t0, 1),
        "search_secs": round(search_secs, 1),
        "overall": {
            "recall@5": round(total["r5"] / total["n"], 4),
            "recall@10": round(total["r10"] / total["n"], 4),
            "recall@20": round(total["r20"] / total["n"], 4),
            "mrr@20": round(total["mrr"] / total["n"], 4),
        },
        "by_type": {
            t: {
                "n": int(b["n"]),
                "recall@5": round(b["r5"] / b["n"], 4),
                "recall@10": round(b["r10"] / b["n"], 4),
                "recall@20": round(b["r20"] / b["n"], 4),
                "mrr@20": round(b["mrr"] / b["n"], 4),
            }
            for t, b in sorted(per_type.items())
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    runs_dir = Path(__file__).parent / "runs"
    runs_dir.mkdir(exist_ok=True)
    tag = f"{'vec' if args.vector else 'lex'}-{len(questions)}q"
    out = args.out or runs_dir / f"retrieval-{tag}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"saved: {out}")


if __name__ == "__main__":
    main()
