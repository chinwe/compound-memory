"""P0-1 prototype benchmark: multi-host concurrent write throughput.

Throwaway measurement for issue #96 P0-1 (prototype, not production): under the
current design (whole-write flock + one git commit per write), how do throughput
and per-write latency behave when several host processes hit the same memory
root at once? Each spawned process builds its own MemoryStore (like separate MCP
hosts), all sharing one root. Lexical mode only (embedder=None) — matches the
production store on this machine and isolates lock + git + index-rewrite cost.

Run from the repo root:
    uv run python experiments/concurrent-write-bench/bench.py \
        --scenario write --git both --processes 1 2 4 8 --ops 40

Scratch roots live under the system temp dir (name contains PROTOTYPE — wipe
any time). The real store (~/.agents/memory) is refused.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import statistics
import tempfile
import time
import uuid
from pathlib import Path

# 词面内容底料：让 tokens.json / BM25 缓存的重写体量接近真实记忆
_WORDS = (
    "memory lock git commit write throughput latency host process namespace "
    "index tokens cache evidence feedback compounding decay archive revive "
    "search rank merge distill proposal review queue contradiction"
).split()


def _content(agent: str, i: int) -> str:
    # 每条内容唯一且带足 token；key=None，避开同 key 冲突/review 队列路径
    k = i % len(_WORDS)
    tail = " ".join(_WORDS[k:] + _WORDS[:k])
    return f"{agent} observation {i}: {tail} {uuid.uuid4().hex[:8]}"


def _worker(
    root: str,
    git: bool,
    proc_idx: int,
    n_ops: int,
    scenario: str,
    hot_id: str,
    barrier: mp.Barrier,
) -> dict:
    """一个子进程 = 一个模拟宿主：独立 MemoryStore 实例，共享同一 root。"""
    from compound_memory.storage import MemoryStore

    agent = f"bench-host-{proc_idx}"
    store = MemoryStore(root, git=git, agent_id=agent)
    # 预热一条不计入统计：物化本进程的缓存态（含各自 git 侧准备）
    store.write(content=_content(agent, 10_000 + proc_idx), type="fact", source=agent)
    barrier.wait(timeout=300)  # 所有宿主同时开写，对齐观测窗口
    latencies: list[float] = []
    first_start = 0.0
    last_end = 0.0
    for i in range(n_ops):
        t0 = time.perf_counter()
        if scenario == "write":
            store.write(content=_content(agent, i), type="fact", source=agent)
        else:
            store.feedback(hot_id, agent)
        t1 = time.perf_counter()
        if i == 0:
            first_start = t0
        last_end = t1
        latencies.append(t1 - t0)
    return {"proc": proc_idx, "latencies": latencies, "first_start": first_start, "last_end": last_end}


def _worker_entry(root, git, proc_idx, n_ops, scenario, hot_id, barrier, q) -> None:
    # Process 入口：结果（纯数据）经队列回传；异常也回传，保证响亮失败
    try:
        q.put(("ok", _worker(root, git, proc_idx, n_ops, scenario, hot_id, barrier)))
    except Exception as exc:  # prototype: 报告后由非零退出兜底
        q.put(("err", f"proc {proc_idx}: {exc!r}"))
        raise


def _pct(sorted_data: list[float], pct: float) -> float:
    if not sorted_data:
        return float("nan")
    idx = min(len(sorted_data) - 1, int(round(pct * (len(sorted_data) - 1))))
    return sorted_data[idx]


def _run_cell(scenario: str, procs: int, n_ops: int, git: bool, seed: int, base: Path) -> dict:
    """一个矩阵格：全新 scratch root，播种 → 对齐起跑 → 并发写 → 汇总。"""
    from compound_memory.storage import MemoryStore

    root = base / f"{scenario}-git{'on' if git else 'off'}-p{procs}-s{seed}-{uuid.uuid4().hex[:4]}"
    hot_id = ""
    t0 = time.perf_counter()
    # 播种用无 git store（无逐条 commit，快）；git 侧由随后的 init commit 一次收口
    seeder = MemoryStore(root, git=False)
    for i in range(seed):
        seeder.write(content=_content("seed-host", i), type="fact", source="seed-host")
    if scenario == "feedback":
        res = seeder.write(
            content="hot memory for concurrent feedback " + uuid.uuid4().hex[:6],
            type="fact",
            source="seed-host",
        )
        hot_id = res["id"]
    seed_s = time.perf_counter() - t0
    if git:
        MemoryStore(root, git=True)  # 首次 init commit（整棵播种树进 git，commit 体量才真实）

    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(procs)
    q = ctx.SimpleQueue()
    children = [
        ctx.Process(target=_worker_entry, args=(str(root), git, idx, n_ops, scenario, hot_id, barrier, q))
        for idx in range(procs)
    ]
    t0 = time.perf_counter()
    for p in children:
        p.start()
    results = [q.get() for _ in children]
    for p in children:
        p.join()
    wall_spawn_s = time.perf_counter() - t0

    failures = [r for tag, r in results if tag == "err"]
    if failures:
        raise SystemExit(f"worker failures: {failures}")
    oks = [r for tag, r in results if tag == "ok"]

    all_lat = sorted(x for r in oks for x in r["latencies"])
    window_start = min(r["first_start"] for r in oks)
    window_end = max(r["last_end"] for r in oks)
    window_s = window_end - window_start
    total_ops = procs * n_ops
    per_proc_mean = {r["proc"]: round(statistics.fmean(r["latencies"]) * 1000, 1) for r in oks}
    return {
        "scenario": scenario,
        "git": "on" if git else "off",
        "procs": procs,
        "seed": seed,
        "ops_per_proc": n_ops,
        "total_ops": total_ops,
        "window_s": round(window_s, 2),
        "throughput_ops_per_s": round(total_ops / window_s, 2),
        "lat_p50_ms": round(_pct(all_lat, 0.50) * 1000, 1),
        "lat_p95_ms": round(_pct(all_lat, 0.95) * 1000, 1),
        "lat_max_ms": round(_pct(all_lat, 1.0) * 1000, 1),
        "per_proc_mean_ms": per_proc_mean,
        "seed_s": round(seed_s, 1),
        "wall_spawn_s": round(wall_spawn_s, 2),
    }


def _check_base(base: Path) -> None:
    # 只允许落在系统临时目录下的 PROTOTYPE 目录；拒绝真实库
    # （macOS 上 /var 是 /private/var 的软链，两侧都要 resolve 再比）
    base = base.resolve()
    if base.parent != Path(tempfile.gettempdir()).resolve() or "PROTOTYPE" not in base.name:
        raise SystemExit(f"unsafe scratch base {base}; expected under tempdir with PROTOTYPE in name")
    real = (Path.home() / ".agents" / "memory").resolve()
    if base == real or base in real.parents or real in base.parents:
        raise SystemExit("refusing to operate near the real store")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", choices=["write", "feedback"], required=True)
    ap.add_argument("--processes", type=int, nargs="+", default=[1, 2, 4, 8])
    ap.add_argument("--git", choices=["on", "off", "both"], default="on")
    ap.add_argument("--ops", type=int, default=40, help="timed ops per process")
    ap.add_argument("--seed", type=int, default=0, help="memories pre-seeded before the run")
    ap.add_argument("--base", type=Path, default=None)
    args = ap.parse_args()

    base = args.base or Path(tempfile.gettempdir()) / "compound-memory-p01-bench-PROTOTYPE"
    _check_base(base)
    base.mkdir(parents=True, exist_ok=True)

    git_modes = [True, False] if args.git == "both" else [args.git == "on"]
    rows = []
    for git in git_modes:
        for procs in args.processes:
            row = _run_cell(args.scenario, procs, args.ops, git, args.seed, base)
            rows.append(row)
            print(
                f"[cell] {args.scenario} git={'on' if git else 'off'} p={procs} seed={args.seed}: "
                f"{row['total_ops']} ops in {row['window_s']}s -> {row['throughput_ops_per_s']} ops/s, "
                f"p50={row['lat_p50_ms']}ms p95={row['lat_p95_ms']}ms max={row['lat_max_ms']}ms",
                flush=True,
            )
    print("\n=== JSON ===")
    import json

    print(json.dumps(rows, indent=2))


if __name__ == "__main__":
    main()
