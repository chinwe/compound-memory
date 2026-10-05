#!/usr/bin/env python3
"""LongMemEval-S(cleaned) → compound-memory 批量灌库（检索评测的准备步）。

一个 session = 一条 episode 记忆（content 为 User/Assistant 轮次拼接），
每题独占一个私有 ns（agent-lme-<qid>），与官方 per-question 评测协议对齐；
created 取 session 真实日期（保留时序，供 recency 先验与时态题使用）。

性能注记：走 store.batch() 批式正门——逐条 write 的校验语义不变，
索引落盘与向量编码收拢批尾一次（逐条 sync 在 tokens.json 全量重写下是
O(n²)，2.4 万条会话不可行）。私有 ns 播种以 ns 属主身份写入（本脚本即
该评测 ns 的创建者）。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

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
    ap.add_argument("--mapping-out", type=Path, default=None, help="mapping 落盘路径（缺省 runs/ 自动命名）")
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
    with store.batch():
        for qi, q in enumerate(questions):
            ns = f"agent-lme-{q['question_id']}"
            for sid, session, raw_date in zip(q["haystack_session_ids"], q["haystack_sessions"], q["haystack_dates"]):
                res = store.write(
                    content=render_session(session),
                    type="episode",
                    source=ns,  # 私有 ns 属主身份：播种器即该评测 ns 的创建者
                    ns=ns,
                    created=iso_date(raw_date) or store.today(),
                )
                mapping[res["id"]] = sid
            if (qi + 1) % 50 == 0:
                print(f"ingested {qi + 1}/{len(questions)} questions ({time.time() - t0:.0f}s)", flush=True)
    # 批尾 flush 已完成词法落盘与向量批量编码（--vector 时），无需再 rebuild_index
    print(f"ingest done: {len(mapping)} sessions total {time.time() - t0:.0f}s")

    runs_dir = Path(__file__).parent / "runs"
    runs_dir.mkdir(exist_ok=True)
    out = args.mapping_out or runs_dir / f"mapping{'-vec' if args.vector else ''}-{len(questions)}q.json"
    out.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
    print(f"mapping: {out}")


if __name__ == "__main__":
    main()
