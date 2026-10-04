#!/usr/bin/env python3
"""LongMemEval-S(cleaned) → compound-memory 批量灌库（检索评测的准备步）。

一个 session = 一条 episode 记忆（content 为 User/Assistant 轮次拼接），
每题独占一个私有 ns（agent-lme-<qid>），与官方 per-question 评测协议对齐；
created 取 session 真实日期（保留时序，供 recency 先验与时态题使用）。

性能注记：不走 store.write()——其逐条 _sync_indexes 在 tokens.json 全量重写
下是 O(n²)，2.4 万条会话不可行；此处直接 _save 落盘，最后一次性 rebuild_index
（词法 + 向量两份缓存全量重建）。experiments 旁路脚本允许使用内部路径。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compound_memory.model import TTL_DAYS, Memory  # noqa: E402
from compound_memory.storage import MemoryStore  # noqa: E402

DATE_RE = re.compile(r"(\d{4})/(\d{2})/(\d{2})")


def iso_date(raw: str) -> str | None:
    m = DATE_RE.search(raw or "")
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def render_session(turns: list[dict]) -> str:
    return "\n".join(
        f"{'User' if t.get('role') == 'user' else 'Assistant'}: {t.get('content', '').strip()}"
        for t in turns
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="ingest LongMemEval sessions into a MemoryStore")
    ap.add_argument("--data", type=Path, default=Path(__file__).parent / "data" / "longmemeval_s_cleaned.json")
    ap.add_argument("--root", type=Path, required=True, help="ephemeral store root (gitignored)")
    ap.add_argument("--limit", type=int, default=None, help="只灌前 N 题（pilot）")
    ap.add_argument("--vector", action="store_true", help="灌库后用真实 embedding 重建向量缓存")
    args = ap.parse_args()

    with open(args.data, encoding="utf-8") as fh:
        questions = json.load(fh)
    if args.limit:
        questions = questions[: args.limit]

    embedder = None
    if args.vector:
        from compound_memory.embedding import auto_encoder

        embedder = auto_encoder()
        if embedder is None:
            raise SystemExit("--vector requires the vec extra + local BGE model (see spec: 索引即缓存)")

    store = MemoryStore(args.root, git=False, embedder=embedder)
    mapping: dict[str, str] = {}  # mem_id -> session_id（评测答案对齐用）
    t0 = time.time()
    for qi, q in enumerate(questions):
        ns = f"agent-lme-{q['question_id']}"
        for sid, session, raw_date in zip(q["haystack_session_ids"], q["haystack_sessions"], q["haystack_dates"]):
            mem = Memory(
                id=store._new_id(),
                ns=ns,
                type="episode",
                source="longmemeval",
                created=iso_date(raw_date) or store._today(),
                content=render_session(session),
                ttl=TTL_DAYS["episode"],
            )
            store._save(mem)
            mapping[mem.id] = sid
        if (qi + 1) % 50 == 0:
            print(f"ingested {qi + 1}/{len(questions)} questions ({time.time() - t0:.0f}s)", flush=True)
    counts = store.rebuild_index()
    print(f"rebuild: {counts} total {time.time() - t0:.0f}s")

    runs_dir = Path(__file__).parent / "runs"
    runs_dir.mkdir(exist_ok=True)
    out = runs_dir / f"mapping{'-vec' if args.vector else ''}-{len(questions)}q.json"
    out.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    print(f"mapping: {out}")


if __name__ == "__main__":
    main()
