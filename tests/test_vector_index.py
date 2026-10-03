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


class RecordingEmbedder:
    """记录全部被编码文本——查询与文档走同一 embedder，只有按文本才能
    断言"对账只编码 diff 的文档"（数调用次数会把查询编码混进来）。"""

    def __init__(self, inner: Callable[[list[str]], list[list[float]]]) -> None:
        self.inner = inner
        self.texts: list[str] = []

    def __call__(self, texts: list[str]) -> list[list[float]]:
        self.texts.extend(texts)
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


class TestIncrementalReconcile:
    """带外增删的读路径自愈必须增量对账：编码成本 = 变更条数，而非全库。

    跨进程场景（另一宿主 write/feedback 一条）是日常路径——若 stale 触发
    全量重编码，库到几百条时首次 search 会阻塞分钟级（spec「索引即缓存」
    的"自动恢复"不能以 O(全库) 编码为代价）。
    """

    def test_cross_process_write_encodes_only_diff(self, tmp_path: Path):
        """未装 vec 的宿主写入后，装 vec 的宿主下一次 search 只编码新增那条。

        真实跨进程场景：带外写入方不更新向量缓存（未装 vec extra / 手编文件），
        读取方 stale 对账——若退化为全量重建，库大后首查阻塞分钟级。
        """
        embedder_b = RecordingEmbedder(bag_embedder_factory())
        a = MemoryStore(  # 无 embedder：写入方不更新 vectors.db（模拟未装 vec extra）
            tmp_path / "memroot", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove
        )
        b = make_vec_store(tmp_path, embedder_b)
        a.write(content="redis persistence 配置要点", type="fact", source="agent-a")
        a.write(content="nginx buffer 大小调优", type="fact", source="agent-a")
        # B 首查：db 缺失 ⇒ 无 diff 基线，全量重建（历史分支，保持不变）。
        # 编码顺序 = scan_pairs 的文件名序（id 含随机 uuid 段），断言用集合不依赖顺序
        b.search("redis 持久化", include_neighbors=False)
        assert embedder_b.texts[0] == "redis 持久化"
        assert {t.strip() for t in embedder_b.texts[1:]} == {
            "redis persistence 配置要点",
            "nginx buffer 大小调优",
        }
        res = a.write(content="docker prune 清理策略", type="fact", source="agent-a")
        hits = b.search("容器磁盘清理", include_neighbors=False)
        assert embedder_b.texts[-1].strip() == "docker prune 清理策略"  # 对账只编码新增那条
        assert len(embedder_b.texts) == 5  # 全量 2 条 + diff 1 条 + 两次查询——前两条未重编码
        assert hits[0]["id"] == res["id"]

    def test_out_of_band_new_file_encodes_only_it(self, tmp_path: Path):
        """绕过 store API 手放的新记忆文件：自愈只编码手放那条，且可被召回。"""
        embedder_b = RecordingEmbedder(bag_embedder_factory())
        a = make_vec_store(tmp_path, bag_embedder_factory())
        b = make_vec_store(tmp_path, embedder_b)
        a.write(content="redis persistence 配置要点", type="fact", source="agent-a")
        b.search("redis 持久化", include_neighbors=False)
        assert embedder_b.texts == ["redis 持久化"]
        hand = a.ns_root / "_shared" / "fact" / "20260101_handmade.md"
        hand.write_text(
            "---\nid: 20260101_handmade\nns: _shared\ntype: fact\n"
            "source: agent-x\ncreated: 2026-01-01\n---\n\nzabbix queue depth alerts\n",
            encoding="utf-8",
        )
        hits = b.search("zabbix queue", include_neighbors=False)
        assert hits[0]["id"] == "20260101_handmade"  # 强匹配排第一（双路 RRF 会带出零相似条，既有行为）
        assert [t.strip() for t in embedder_b.texts] == [
            "redis 持久化",
            "zabbix queue",
            "zabbix queue depth alerts",
        ]

    def test_out_of_band_delete_removed_from_index(self, tmp_path: Path):
        """带外删除的记忆文件：对账后不再被向量路召回，其余不受影响。"""
        embedder_b = CountingEmbedder(bag_embedder_factory())
        a = make_vec_store(tmp_path, bag_embedder_factory())
        b = make_vec_store(tmp_path, embedder_b)
        kept = a.write(content="redis persistence 配置要点", type="fact", source="agent-a")
        gone = a.write(content="memcached 线程模型", type="fact", source="agent-a")
        b.search("缓存", include_neighbors=False)  # 建立基线
        sandbox_safe_remove(a.ns_root / "_shared" / "fact" / f"{gone['id']}.md")
        hits = b.search("缓存线程模型", include_neighbors=False)
        assert gone["id"] not in [h["id"] for h in hits]
        assert [h["id"] for h in hits] == [kept["id"]]

    def test_out_of_band_move_recall_relocates(self, tmp_path: Path):
        """手编挪位（文件换 type 目录、内容不变）：对账修正 rel_path 后向量路仍召回。

        钉住直读语义的行为等价——向量召回按 knn 返回的 rel_path 直读文件，
        不再逐 hit find()（rglob 全库）；挪位的修正完全依赖 stale 对账。
        """
        import os

        a = make_vec_store(tmp_path, bag_embedder_factory())
        b = make_vec_store(tmp_path, bag_embedder_factory())
        res = a.write(content="redis persistence 配置要点", type="fact", source="agent-a")
        src = a.ns_root / "_shared" / "fact" / f"{res['id']}.md"
        dst = a.ns_root / "_shared" / "episode" / src.name
        os.replace(src, dst)
        hits = b.search("redis 持久化", include_neighbors=False)
        assert [h["id"] for h in hits] == [res["id"]]
