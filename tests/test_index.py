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


class TestStoreSeam:
    """store 层只断言 README 记载的 layout 存在性与行为，不解析缓存内容。"""

    def test_write_activates_cache(self, store):
        """写入必须激活缓存——这是 store 承诺"写路径保持缓存存活"的可观察面。"""
        cache = store.root / "index" / "tokens.json"
        assert not cache.exists()
        store.write(content="索引激活测试 Vercel 部署", type="episode", source="agent-a")
        assert cache.exists()

    def test_cache_loss_degrades_to_scan(self, store):
        """缓存丢失降级到慢，绝不报错。"""
        mem = store.write(content="Docker 网络模式 bridge", type="episode", source="agent-a")
        os.replace(store.root / "index", store.root / "index.deleted")
        hits = store.search("Docker bridge")
        assert [h["id"] for h in hits] == [mem["id"]]

    def test_scan_fallback_finds_out_of_band_memory(self, store):
        """绕过 store API 手工放置的记忆文件（模拟外部编辑/git 操作）仍可被检索：
        索引不含其词条 ⇒ 候选为空 ⇒ 扫描兜底接管。"""
        store.write(content="redis cache eviction policy", type="episode", source="agent-a")
        hand = store.ns_root / "_shared" / "fact" / "20260101_handmade.md"
        hand.write_text(
            "---\nid: 20260101_handmade\nns: _shared\ntype: fact\n"
            "source: agent-x\ncreated: 2026-01-01\n---\n\nzabbix queue depth alerts\n",
            encoding="utf-8",
        )
        hits = store.search("zabbix queue")
        assert [h["id"] for h in hits] == ["20260101_handmade"]
