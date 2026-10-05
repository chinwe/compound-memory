"""store.batch() 批式落库通道的行为测试。

测试编码"为什么重要"：批量灌库/蒸馏落库曾是逐条 write——每条全量重写
tokens.json + 一次 git commit，合计 O(n²)，旁路脚本（experiments/longmemeval）
被迫翻墙用私有件绕过。batch() 把校验逐条照走、commit 与索引 flush 收拢批尾，
让旁路回到正门。失败语义是"落地即已提交"：批内异常时已写入条目照常提交并
上抛，不存在静默半提交——任何一步被静默跳过都算实现错了。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import pytest

from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE, bag_embedder_factory, sandbox_safe_remove


class CountingEmbedder:
    """包装 embedder 统计调用次数——验证批尾一次性批量编码。"""

    def __init__(self, inner: Callable[[list[str]], list[list[float]]]) -> None:
        self.inner = inner
        self.calls = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return self.inner(texts)


class TestBatchCommitGranularity:
    def test_batch_commits_once(self, store: MemoryStore):
        """N 条写入恰好一次 commit（逐条 write 是 N 次——O(n²) 的来源之一）。"""
        store.write(content="批前基线条目", type="fact", source="agent-a")
        before = len(store.git_log(50))
        with store.batch():
            for i in range(3):
                store.write(content=f"批内条目 {i} redis queue", type="fact", source="agent-a")
        log = store.git_log(50)
        assert len(log) == before + 1
        assert "batch write 3 entries" in log[0]

    def test_empty_batch_commits_nothing(self, store: MemoryStore):
        before = len(store.git_log(50))
        with store.batch():
            pass
        assert len(store.git_log(50)) == before

    def test_custom_message_is_honored(self, store: MemoryStore):
        with store.batch(message="distill apply demo"):
            store.write(content="溯源消息自定义条目", type="fact", source="agent-a")
        assert "distill apply demo" in store.git_log(1)[0]

    def test_batch_entries_searchable_after_exit(self, store: MemoryStore):
        with store.batch():
            store.write(content="vercel serverless 超时配置要点", type="fact", source="agent-a")
        assert [h["id"] for h in store.search("vercel serverless")]


class TestBatchFailureSemantics:
    def test_exception_partial_commits_and_reraises(self, store: MemoryStore):
        """批内异常：已写入条目照常提交（注明 partial）并原样上抛——不静默吞。"""
        with pytest.raises(RuntimeError, match="boom"):
            with store.batch():
                store.write(content="已落盘第一条 zabbix", type="fact", source="agent-a")
                store.write(content="已落盘第二条 zabbix", type="fact", source="agent-a")
                raise RuntimeError("boom")
        assert "batch write 2 entries (partial)" in store.git_log(1)[0]
        assert len(store.search("已落盘 zabbix")) == 2

    def test_exception_before_any_write_commits_nothing(self, store: MemoryStore):
        before = len(store.git_log(50))
        with pytest.raises(RuntimeError, match="boom"):
            with store.batch():
                raise RuntimeError("boom")
        assert len(store.git_log(50)) == before

    def test_nested_batch_rejected(self, store: MemoryStore):
        with store.batch():
            with pytest.raises(ValueError, match="nested"):
                with store.batch():
                    pass


class TestBatchDefersAllVerbs:
    def test_decay_sweep_inside_batch_defers_commit(self, store: MemoryStore):
        """批内任何动词的 _commit 都延迟（单点拦截）：decay 归档也收进批尾一次提交。"""
        # insight 归档 TTL 180 天：created 2026-01-01 距 CLOCK_DATE 273 天且 uses=0，必被衰减
        mem = store.write(
            content="长期未用待衰减条目", type="insight", source="agent-a", created="2026-01-01"
        )
        before = len(store.git_log(50))
        with store.batch():
            store.decay_sweep()
        log = store.git_log(50)
        assert len(log) == before + 1
        assert "batch write 1 entries" in log[0]
        got = store.get(mem["id"], include_neighbors=False)
        assert got["archived"] is True

    def test_deferred_vector_encode_batches_into_one_call(self, tmp_path: Path):
        """向量路批尾一次性批量编码：3 条写入 1 次 embedder 调用（逐条 sync 是 3 次）。"""
        embedder = CountingEmbedder(bag_embedder_factory())
        store = MemoryStore(
            tmp_path / "memroot",
            clock=lambda: CLOCK_DATE,
            remover=sandbox_safe_remove,
            embedder=embedder,
        )
        with store.batch():
            for i in range(3):
                store.write(content=f"docker network 模式说明 {i}", type="fact", source="agent-a")
        assert embedder.calls == 1
        assert len(store.search("docker network 模式", include_neighbors=False)) == 3


class TestBatchScale:
    def test_flush_count_is_independent_of_batch_size(self, tmp_path: Path):
        """批式写的 tokens.json 落盘次数与批量大小无关——二次方回归在此被结构性钉住。

        逐条 _upsert 全量重写 tokens.json 是灌库 O(n²) 的根源；批尾 flush 一次。
        用 sys 审计钩子数落盘打开次数（黑盒可观测，不吃墙钟抖动），比较两个批量
        的计数：逐条路径计数随 N 线性增长，批式路径恒定（tmp 写 + replace 各一次）。
        钩子对本进程残留是良性的（startswith+endswith 判断，进程内开销可忽略；
        按各 store 路径前缀过滤，互不串扰）。
        """

        def _flush_opens(store_root: Path, n: int) -> list[str]:
            store = MemoryStore(store_root, git=False, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
            seen: list[str] = []

            def _count_tokens_save(event: str, args: tuple) -> None:
                # Index._save 原子写先落 tokens.tmp（with_suffix 替换掉 .json）再 replace
                if (
                    event == "open"
                    and isinstance(args[0], str)
                    and args[0].startswith(str(store.root / "index"))
                    and args[0].endswith("tokens.tmp")
                ):
                    seen.append(args[0])

            sys.addaudithook(_count_tokens_save)
            with store.batch():
                for i in range(n):
                    store.write(content=f"zzmarker{i} batch scale item", type="episode", source="agent-a")
            return seen

        small = _flush_opens(tmp_path / "memroot-small", 20)
        large = _flush_opens(tmp_path / "memroot-large", 80)
        assert len(small) >= 1
        assert len(large) == len(small), "落盘次数必须与批量大小无关（逐条路径是 80 > 20）"
        hits = MemoryStore(
            tmp_path / "memroot-large", git=False, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove
        ).search("zzmarker79")
        assert [h["id"] for h in hits], "重开 store 后批写条目必须可检索（flush 落盘完整）"
