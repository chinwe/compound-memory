"""perf-bench：compound-memory 关键路径的分规模延迟基准。

只测延迟不测召回质量（质量回归见 experiments/vec-spike）。
库内容来自同目录 gen_mock（主题簇结构，词面候选集大小可解释）。

用法：
    uv run --extra vec python experiments/perf-bench/bench.py --scales 100 500 1000
    uv run python experiments/perf-bench/bench.py --scales 100   # 未装 vec extra 自动降级纯词面

场景与数字解读见同目录 README.md。数据落在系统临时目录（不动仓库与真实库）。
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from compound_memory.embedding import auto_encoder  # noqa: E402
from compound_memory.storage import MemoryStore  # noqa: E402

from gen_mock import BROAD_QUERIES, NARROW_QUERIES, SEMANTIC_QUERIES, make_memories, seed_store  # noqa: E402

REAL_MEMORY_ROOT = Path.home() / ".agents" / "memory"

# 表格行顺序（run_scale 返回的 dict 键按此排序输出）
SCENARIO_ORDER = [
    "seed n memories (total)",
    "search lexical narrow, no neighbors (med/search)",
    "search lexical narrow, default neighbors (med/search)",
    "search lexical broad, default (med/search)",
    "search vector semantic, default (med/search)",
    "write + sync encode + git commit (med)",
    "feedback, no re-encode (med)",
    "reconcile after oob write (med/search)",
    "stats full scan (total)",
    "full rebuild-index (total)",
]


def timed_ms(fn: Callable[[], Any], repeat: int, per_call: int = 1) -> float:
    """重复执行取中位数（ms）；per_call 为单次 fn 包含的等效操作数（折算成单操作）。"""
    samples = []
    for _ in range(repeat):
        t0 = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t0) * 1000.0 / per_call)
    return statistics.median(samples)


def bench_search(store: MemoryStore, queries: list[str], repeat: int, **kwargs: Any) -> float:
    """轮换查询取中位数，折算为单次 search 延迟。"""
    return timed_ms(lambda: [store.search(q, **kwargs) for q in queries], repeat, per_call=len(queries))


def bench_reconcile(root: Path, store: MemoryStore, tag: int) -> float:
    """带外写 1 个 .md 后 search：跨进程写后首查的日常形态（增量对账路径）。"""
    times = []
    for i in range(3):
        oob = root / "namespaces" / "_shared" / "fact" / f"20300101_oob{tag}x{i}.md"
        oob.write_text(
            "---\n"
            f"id: 20300101_oob{tag}x{i}\nns: _shared\ntype: fact\nsource: bench-oob\n"
            "created: 2030-01-01\n---\n\n"
            "oob reconcile probe entry，涉及配置、部署的取舍，缓存淘汰相关注意事项。\n",
            encoding="utf-8",
        )
        t0 = time.perf_counter()
        store.search("oob reconcile probe 配置", include_neighbors=False)
        times.append((time.perf_counter() - t0) * 1000.0)
    return statistics.median(times)


def run_scale(n: int, base_root: Path) -> dict[str, str]:
    """在独立 root 上跑完一个规模档，返回 场景 → 格式化数值。"""
    root = base_root / f"n{n}"
    embedder = auto_encoder()

    t0 = time.perf_counter()
    ids = seed_store(root, n)
    seed_secs = time.perf_counter() - t0

    # bench store 与生产同款：git 开启 + 向量路（embedder 可用时）
    store = MemoryStore(root, git=True, embedder=embedder)
    store.search("warmup 配置", include_neighbors=False)  # 暖机建缓存基线，不计入

    extra = make_memories(5, seed=n + 1)
    out: dict[str, str] = {"seed n memories (total)": f"{seed_secs:.1f}s"}
    out["search lexical narrow, no neighbors (med/search)"] = (
        f"{bench_search(store, NARROW_QUERIES, repeat=6, include_neighbors=False):.1f}ms"
    )
    out["search lexical narrow, default neighbors (med/search)"] = (
        f"{bench_search(store, NARROW_QUERIES, repeat=6):.1f}ms"
    )
    out["search lexical broad, default (med/search)"] = f"{bench_search(store, BROAD_QUERIES, repeat=6):.1f}ms"
    out["search vector semantic, default (med/search)"] = f"{bench_search(store, SEMANTIC_QUERIES, repeat=6):.1f}ms"
    out["write + sync encode + git commit (med)"] = f"{timed_ms(lambda: store.write(**extra.pop()), 5):.1f}ms"
    out["feedback, no re-encode (med)"] = f"{timed_ms(lambda: store.feedback(ids.pop(), 'bench-agent'), 5):.1f}ms"
    out["reconcile after oob write (med/search)"] = f"{bench_reconcile(root, store, tag=n):.1f}ms"
    out["stats full scan (total)"] = f"{timed_ms(store.stats, 1) / 1000.0:.2f}s"
    out["full rebuild-index (total)"] = fmt_ms(timed_ms(store.rebuild_index, 1))
    return out


def fmt_ms(v: float) -> str:
    return f"{v:.1f}ms" if v < 10000 else f"{v / 1000.0:.2f}s"


def print_table(results: dict[int, dict[str, str]], mode: str) -> None:
    print(f"\nmode: {mode}")
    header = f"{'scenario':<48}" + "".join(f"{f'N={n}':>12}" for n in results)
    print(header)
    print("-" * len(header))
    for key in SCENARIO_ORDER:
        row = f"{key:<48}" + "".join(f"{results[n].get(key, '-'):>12}" for n in results)
        print(row)
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="Latency benchmark for compound-memory key paths")
    parser.add_argument("--scales", type=int, nargs="+", default=[100, 500], help="library sizes to bench")
    parser.add_argument("--root", type=Path, default=None, help="base dir for bench stores (default: system temp)")
    args = parser.parse_args()
    base_root = args.root or Path(tempfile.gettempdir()) / "compound-memory-bench"
    if base_root.resolve() == REAL_MEMORY_ROOT.resolve() or REAL_MEMORY_ROOT.resolve() in base_root.resolve().parents:
        print(f"refusing to bench inside the real memory root: {REAL_MEMORY_ROOT}", file=sys.stderr)
        return 1
    embedder = auto_encoder()
    mode = "vec (BGE-small-zh int8, CPU)" if embedder else "lexical (vec extra / model missing, degraded)"
    results: dict[int, dict[str, str]] = {}
    for n in args.scales:
        t0 = time.perf_counter()
        results[n] = run_scale(n, base_root)
        print(f"[scale {n}] done in {time.perf_counter() - t0:.0f}s -> {base_root / f'n{n}'}", file=sys.stderr)
    print_table(results, mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
