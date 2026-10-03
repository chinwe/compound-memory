"""Index 模块测试。

分层归位（深化后的测试 seam）：
- Index 单元测试：直接构造 Index（注入 scan_pairs 假实现，不需要 MemoryStore），
  读自己的落盘文件 index/tokens.json 在这里是合法的——持久化文件就是它的状态。
- store seam 测试：只走 README 记载的 layout（存在性断言）与行为断言，
  不解析缓存 JSON 内容。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from compound_memory.index import Index
from compound_memory.model import Memory
from compound_memory.scoring import tokenize
from compound_memory.storage import MemoryStore

from conftest import sandbox_safe_remove


def make_mem(seq: int, content: str, archived: bool = False) -> Memory:
    return Memory(
        id=f"20260101_{seq:06d}",
        ns="_shared",
        type="episode",
        source="agent-a",
        created="2026-01-01",
        content=content,
        archived=archived,
    )


def rel_of(mem: Memory) -> str:
    return f"namespaces/_shared/episode/{mem.id}.md"


def cache_file(root: Path) -> Path:
    return root / "index" / "tokens.json"


@pytest.fixture
def index(tmp_path: Path):
    """scan_pairs 假实现：返回测试手工维护的 pairs 列表，模拟 store 活动区扫描。"""
    pairs: list[tuple[Memory, str]] = []
    return Index(tmp_path, scan_pairs=lambda: list(pairs)), pairs


class TestSync:
    def test_active_memory_is_indexed(self, index, tmp_path: Path):
        """sync 是唯一变更动词：active ⇒ rel_path 进入索引。"""
        idx, _ = index
        mem = make_mem(1, "vercel timeout rules")
        idx.sync(mem, rel_of(mem))
        data = json.loads(cache_file(tmp_path).read_text(encoding="utf-8"))
        assert rel_of(mem) in data["vercel"]

    def test_archived_memory_is_un_indexed(self, index, tmp_path: Path):
        """不变量的家：archived ⇒ 移出索引。调用方不再自己选 upsert 还是 remove。"""
        idx, _ = index
        mem = make_mem(1, "vercel timeout rules")
        idx.sync(mem, rel_of(mem))
        mem.archived = True
        idx.sync(mem, rel_of(mem))
        data = json.loads(cache_file(tmp_path).read_text(encoding="utf-8"))
        assert all(rel_of(mem) not in paths for paths in data.values())

    def test_sync_is_idempotent(self, index, tmp_path: Path):
        """同一 rel_path 重复 sync（write 后 feedback 的真实序列）不产生重复词条。"""
        idx, _ = index
        mem = make_mem(1, "vercel timeout rules")
        idx.sync(mem, rel_of(mem))
        idx.sync(mem, rel_of(mem))
        data = json.loads(cache_file(tmp_path).read_text(encoding="utf-8"))
        assert data["vercel"].count(rel_of(mem)) == 1


class TestCandidatesAndRebuild:
    def test_candidates_finds_paths_by_any_token(self, index):
        idx, _ = index
        mem_a = make_mem(1, "redis queue depth")
        mem_b = make_mem(2, "docker network bridge")
        idx.sync(mem_a, rel_of(mem_a))
        idx.sync(mem_b, rel_of(mem_b))
        assert idx.candidates(tokenize("redis")) == [rel_of(mem_a)]
        assert idx.candidates(tokenize("redis docker")) == [rel_of(mem_a), rel_of(mem_b)]

    def test_rebuild_replaces_previous_content(self, index):
        idx, _ = index
        mem_a = make_mem(1, "redis queue depth")
        idx.sync(mem_a, rel_of(mem_a))
        mem_b = make_mem(2, "docker network bridge")
        counts = idx.rebuild([(mem_b, rel_of(mem_b))])
        assert counts == {"memories": 1, "tokens": len(set(tokenize("docker network bridge")))}
        assert idx.candidates(tokenize("redis")) == []
        assert idx.candidates(tokenize("docker")) == [rel_of(mem_b)]


class TestSelfHealing:
    def test_missing_cache_rebuilds_on_next_access(self, index, tmp_path: Path):
        """缓存文件丢失 ⇒ 下一次访问经 scan_pairs 全量重建，调用方无感。"""
        idx, pairs = index
        mem_a = make_mem(1, "redis queue depth")
        idx.sync(mem_a, rel_of(mem_a))
        pairs.append((mem_a, rel_of(mem_a)))
        os.replace(cache_file(tmp_path), tmp_path / "tokens.json.gone")
        mem_b = make_mem(2, "docker network bridge")
        idx.sync(mem_b, rel_of(mem_b))
        assert idx.candidates(tokenize("redis")) == [rel_of(mem_a)]
        assert idx.candidates(tokenize("docker")) == [rel_of(mem_b)]

    def test_corrupt_cache_rebuilds_on_next_access(self, index, tmp_path: Path):
        idx, pairs = index
        mem_a = make_mem(1, "redis queue depth")
        pairs.append((mem_a, rel_of(mem_a)))
        idx.sync(mem_a, rel_of(mem_a))
        cache_file(tmp_path).write_text("{not json", encoding="utf-8")
        assert idx.candidates(tokenize("redis")) == [rel_of(mem_a)]

    def test_second_instance_sees_first_instance_writes(self, tmp_path: Path):
        """跨进程活性：缓存属于 store，不属于进程。

        长驻进程 B 已加载缓存后，进程 A 的写入（更新 tokens.json）必须对 B 的
        下一次检索可见——旧实现里 B 的内存态永不失效，A 写的记忆静默丢失。
        """
        pairs: list[tuple[Memory, str]] = []
        a = Index(tmp_path, scan_pairs=lambda: list(pairs))
        b = Index(tmp_path, scan_pairs=lambda: list(pairs))
        assert b.candidates(tokenize("redis")) == []  # b 先加载并落盘空缓存（建立基线）
        mem = make_mem(1, "redis queue depth")
        a.sync(mem, rel_of(mem))  # a（另一进程）写入并更新缓存文件
        assert b.candidates(tokenize("redis")) == [rel_of(mem)]  # b 检测 mtime 变化后重载


class TestStoreSeam:
    """store 层只断言 README 记载的 layout 存在性与行为，不解析缓存内容。"""

    def test_write_activates_cache(self, store):
        """写入必须激活缓存——这是 store 承诺"写路径保持缓存存活"的可观察面。"""
        cache = store.root / "index" / "tokens.json"
        assert not cache.exists()
        store.write(content="索引激活测试 Vercel 部署", type="episode", source="agent-a")
        assert cache.exists()

    def test_cache_loss_degrades_to_rebuild(self, store):
        """缓存目录整体丢失 ⇒ 下次检索经全量扫描自动重建，绝不报错。"""
        mem = store.write(content="Docker 网络模式 bridge", type="episode", source="agent-a")
        os.replace(store.root / "index", store.root / "index.deleted")
        hits = store.search("Docker bridge")
        assert [h["id"] for h in hits] == [mem["id"]]

    def test_out_of_band_new_file_is_auto_reindexed(self, store):
        """绕过 store API 手工放置的新记忆文件（模拟外部编辑/git 操作）仍可被检索：
        新增文件更新父目录 mtime ⇒ Index 检测缓存落后 ⇒ 全量重建。"""
        store.write(content="redis cache eviction policy", type="episode", source="agent-a")
        hand = store.ns_root / "_shared" / "fact" / "20260101_handmade.md"
        hand.write_text(
            "---\nid: 20260101_handmade\nns: _shared\ntype: fact\n"
            "source: agent-x\ncreated: 2026-01-01\n---\n\nzabbix queue depth alerts\n",
            encoding="utf-8",
        )
        hits = store.search("zabbix queue")
        assert [h["id"] for h in hits] == ["20260101_handmade"]

    def test_out_of_band_overlapping_token_no_longer_blind(self, store):
        """旧实现的盲区（回归钉）：带外记忆与已索引记忆共享任一 token 时，
        "索引部分命中即短路"曾让它永久隐形——活性自愈后中间态不复存在。"""
        store.write(content="redis cache eviction policy", type="episode", source="agent-a")
        hand = store.ns_root / "_shared" / "fact" / "20260101_overlap.md"
        hand.write_text(
            "---\nid: 20260101_overlap\nns: _shared\ntype: fact\n"
            "source: agent-x\ncreated: 2026-01-01\n---\n\nredis queue depth alerts\n",
            encoding="utf-8",
        )
        hits = store.search("redis queue")
        assert "20260101_overlap" in [h["id"] for h in hits]


class TestIncrementalReconcile:
    """带外增删走增量对账（不再全量重建）：正确性基准 = 与全量重建集合等价。

    对账是性能优化，测试钉的是用户可见行为不变：新增可召回、删除即消失、
    挪位 relocates，且对账后的缓存内容与 rebuild 逐集合一致。
    """

    def _cache_map(self, store: MemoryStore) -> dict[str, set[str]]:
        raw = json.loads((store.root / "index" / "tokens.json").read_text(encoding="utf-8"))
        return {tok: set(rels) for tok, rels in raw.items()}

    def test_reconcile_matches_full_rebuild(self, store: MemoryStore):
        """带外加删后对账的缓存与全量重建集合等价——增量的正确性基准。"""
        store.write(content="redis cache eviction policy", type="fact", source="agent-a")
        gone = store.write(content="memcached threading model", type="fact", source="agent-a")
        sandbox_safe_remove(store.ns_root / "_shared" / "fact" / f"{gone['id']}.md")
        hand = store.ns_root / "_shared" / "fact" / "20260101_handmade.md"
        hand.write_text(
            "---\nid: 20260101_handmade\nns: _shared\ntype: fact\n"
            "source: agent-x\ncreated: 2026-01-01\n---\n\nzabbix queue depth alerts\n",
            encoding="utf-8",
        )
        store.search("redis cache")  # 触发带外自愈（对账路径）
        reconciled = self._cache_map(store)
        store.index.rebuild(store._scan_pairs())
        assert reconciled == self._cache_map(store)

    def test_out_of_band_delete_drops_from_recall(self, store: MemoryStore):
        """带外删除：对账后不再召回（词条清除，不残留）。"""
        kept = store.write(content="redis cache eviction policy", type="fact", source="agent-a")
        gone = store.write(content="memcached threading model", type="fact", source="agent-a")
        sandbox_safe_remove(store.ns_root / "_shared" / "fact" / f"{gone['id']}.md")
        assert store.search("memcached threading") == []
        assert [h["id"] for h in store.search("redis cache")] == [kept["id"]]

    def test_out_of_band_move_relocates_recall(self, store: MemoryStore):
        """手编挪位（换 type 目录、内容不变）：对账后新位置仍召回。"""
        res = store.write(content="docker network bridge mode", type="fact", source="agent-a")
        src = store.ns_root / "_shared" / "fact" / f"{res['id']}.md"
        dst = store.ns_root / "_shared" / "episode" / src.name
        os.replace(src, dst)
        assert [h["id"] for h in store.search("docker network bridge")] == [res["id"]]


class TestFrontmatterLoaderParity:
    """C 扩展 loader 与纯 Python SafeLoader 语义逐位一致——换 loader 是纯性能改动。"""

    # 覆盖 _save round-trip 的类型面：引号保护日期串、float/int、bool、
    # 列表、null、含中文与冒号的值（safe_dump 会加引号）
    SAMPLES = [
        "id: 20261003_x\nns: _shared\ntype: fact\nsource: 'agent-zcode'\n"
        "created: '2026-10-03'\nconfidence: 0.75\nuses: 3\nttl: 180\n",
        "id: y\nlinks:\n- a\n- b\nvalidated_by:\n- agent-a\n- agent-b\narchived: true\n",
        "id: z\nkey: 'proj/向量: 索引'\norigin: null\nlast_used: '2026-10-01'\nconfidence: 1.0\n",
    ]

    def test_c_loader_matches_safe_loader(self):
        import yaml

        if not yaml.__with_libyaml__:
            pytest.skip("PyYAML built without libyaml (C loader absent)")
        for fm in self.SAMPLES:
            assert yaml.load(fm, Loader=yaml.CSafeLoader) == yaml.load(fm, Loader=yaml.SafeLoader)
