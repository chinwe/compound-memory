"""向量缓存与向量召回路的行为测试（fake embedder，不依赖 onnxruntime/真模型）。

测试编码"为什么重要"：向量索引与词法 Index 同守"活动必在、归档必不在、
缓存可重建"的不变量（spec story 16/17）；写入路径必须零重复编码（feedback
不炸写入延迟）；embedder 缺席时检索必须静默降级纯词面（检索降级不报错）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE, bag_embedder_factory, sandbox_safe_remove


class CountingEmbedder:
    """包装 embedder 统计调用次数——验证 hash 未变时 sync 零重复编码。"""

    def __init__(self, inner: Callable[[list[str]], list[list[float]]]) -> None:
        self.inner = inner
        self.calls = 0

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return self.inner(texts)


def make_vec_store(tmp_path: Path, embedder: Callable[[list[str]], list[list[float]]]) -> MemoryStore:
    return MemoryStore(
        tmp_path / "memroot", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove, embedder=embedder
    )


class TestVectorRecallBehavior:
    def test_write_then_semantic_search_recalls(self, vec_store: MemoryStore, tmp_path: Path):
        """写入 → 语义改写搜索能召回（向量召回路存在的全部意义）。"""
        res = vec_store.write(content="用户偏好表格加要点的结构化输出", type="fact", source="agent-a")
        hits = vec_store.search("输出格式上有什么讲究")  # 词面近零重叠，靠向量路
        assert [h["id"] for h in hits] == [res["id"]]

    def test_vec_candidates_fuse_with_lexical_in_rank(self, vec_store: MemoryStore):
        """双路并集：词面零命中的记忆经向量路进入结果，且共享同一结果形状。"""
        res = vec_store.write(content="nginx buffer 配置要点", type="fact", source="agent-a")
        hits = vec_store.search("反向代理的缓冲区设置", include_neighbors=False)
        shape = {"id", "score", "similarity", "confidence", "uses", "type", "ns", "source", "content"}
        assert hits and set(hits[0]) == shape
        assert hits[0]["id"] == res["id"]

    def test_lexical_only_store_is_unchanged(self, store: MemoryStore):
        """无 embedder 的 store 与历史行为一致：词面零命中 ⇒ 空结果（不引入回归）。"""
        store.write(content="nginx buffer 配置要点", type="fact", source="agent-a")
        assert store.search("反向代理的缓冲区设置") == []


class TestVectorIndexInvariants:
    def test_archived_memory_not_in_vector_index(self, vec_store: MemoryStore):
        """归档必不在索引：archive 后同一查询不得再召回（Index 不变量同构）。"""
        res = vec_store.write(content="vercel serverless 超时拆分", type="fact", source="agent-a")
        vec_store._archive(vec_store.find(res["id"]))
        assert vec_store.search("serverless 函数执行时间上限") == []

    def test_revive_returns_to_vector_index(self, vec_store: MemoryStore):
        """复活可逆：revive 后向量路恢复召回。"""
        res = vec_store.write(content="vercel serverless 超时拆分", type="fact", source="agent-a")
        vec_store._archive(vec_store.find(res["id"]))
        vec_store.revive(res["id"])
        assert [h["id"] for h in vec_store.search("serverless 函数执行时间上限")] == [res["id"]]

    def test_db_loss_degrades_to_rebuild(self, vec_store: MemoryStore, tmp_path: Path):
        """删除 vectors.db 后 search 仍正常（spec story 16 验收的向量版）。"""
        res = vec_store.write(content="docker prune 清理策略", type="fact", source="agent-a")
        db = tmp_path / "memroot" / "index" / "vectors.db"
        assert db.exists()
        sandbox_safe_remove(db)
        assert [h["id"] for h in vec_store.search("容器磁盘清理")] == [res["id"]]

    def test_feedback_skips_reembedding(self, vec_store: MemoryStore, tmp_path: Path):
        """feedback 只动 conf/uses/last_used——内容 hash 未变，不得重复编码。"""
        embedder = CountingEmbedder(bag_embedder_factory())
        store = make_vec_store(tmp_path, embedder)
        res = store.write(content="redis persistence 配置", type="fact", source="agent-a")
        assert embedder.calls == 1  # 写入编码一次
        store.feedback(res["id"], "agent-b")
        assert embedder.calls == 1  # feedback 零编码

    def test_content_edit_reembeds(self, vec_store: MemoryStore, tmp_path: Path):
        """内容变更（带外手编后显式 rebuild）走重编码——hash 校验的唯一翻新路径。"""
        embedder = CountingEmbedder(bag_embedder_factory())
        store = make_vec_store(tmp_path, embedder)
        res = store.write(content="旧内容 v1", type="fact", source="agent-a")
        store.rebuild_index()
        assert embedder.calls == 2

    def test_ns_scoped_vector_recall(self, vec_store: MemoryStore):
        """ns 过滤对向量候选同样生效：私有 ns 记忆不出现在 _shared 搜索里。"""
        vec_store.write(content="内部代号 TARS 仅 workbuddy 可用", type="fact", source="workbuddy", ns="agent-workbuddy")
        vec_store.write(content="共享的部署注意事项", type="fact", source="agent-a")
        hits = vec_store.search("助手的名字设定")
        assert all(h["ns"] == "_shared" for h in hits)

    def test_rebuild_index_counts_both_caches(self, vec_store: MemoryStore):
        """rebuild-index 双缓存重建：词法与向量计数合并返回。"""
        vec_store.write(content="redis persistence 配置", type="fact", source="agent-a")
        counts = vec_store.rebuild_index()
        assert counts["memories"] == 1
        assert "tokens" in counts  # 词法缓存计数
