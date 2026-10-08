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
import time
from pathlib import Path

import pytest

from compound_memory.index import CACHE_VERSION, Index
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
        assert rel_of(mem) in data["index"]["vercel"]

    def test_archived_memory_is_un_indexed(self, index, tmp_path: Path):
        """不变量的家：archived ⇒ 移出索引。调用方不再自己选 upsert 还是 remove。"""
        idx, _ = index
        mem = make_mem(1, "vercel timeout rules")
        idx.sync(mem, rel_of(mem))
        mem.archived = True
        idx.sync(mem, rel_of(mem))
        data = json.loads(cache_file(tmp_path).read_text(encoding="utf-8"))
        assert all(rel_of(mem) not in paths for paths in data["index"].values())

    def test_sync_is_idempotent(self, index, tmp_path: Path):
        """同一 rel_path 重复 sync（write 后 feedback 的真实序列）不产生重复词条。"""
        idx, _ = index
        mem = make_mem(1, "vercel timeout rules")
        idx.sync(mem, rel_of(mem))
        idx.sync(mem, rel_of(mem))
        data = json.loads(cache_file(tmp_path).read_text(encoding="utf-8"))
        assert data["index"]["vercel"].count(rel_of(mem)) == 1


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

        b 建基线后把缓存 mtime 压到过去：本测试两次 _save 之间零间隔（纯内存
        操作相接），同 tick 内 mtime_ns 相同会让判活漏检（2026-10-05 审计三次
        全量跑出一次 flaky，探针已确定性复现）——压 mtime 在构造上消除竞态，
        使「A 写入 → B 检测」必然跨 tick。压过之后 b 先空重载一次（内容未变，
        无害），随后 a 的新写入 mtime 必然不同。全文件仅此测试是零间隔双写：
        store seam 测试的间隔里有 git commit 子进程兜底，无需同款处理。

        Windows 适配（#55 CI 实证）：空重载一步不可省——省了 b 的基线 stamp
        是压之前的真实 now，与 a 的写入同处亚 tick 邻域，Windows 的 FILETIME
        粒度/元数据可见性会让「缓存变了」与「目录变了」的判定顺序平台化；
        压后重载让 stamp 落在 60 秒前的 past，a 的写入必然跨任何粒度的 tick。
        scan 桩在 sync 后供给同一记忆：即便 dirs 探测触发 reconcile，对账
        diff 为空、不会清掉刚重载的条目（macOS 上 reconcile 本就不触发，
        走缓存重载路径，断言语义不变）。
        """
        pairs: list[tuple[Memory, str]] = []
        a = Index(tmp_path, scan_pairs=lambda: list(pairs))
        b = Index(tmp_path, scan_pairs=lambda: list(pairs))
        assert b.candidates(tokenize("redis")) == []  # b 先加载并落盘空缓存（建立基线）
        past = time.time() - 60
        os.utime(cache_file(tmp_path), (past, past))
        assert b.candidates(tokenize("redis")) == []  # 压 mtime 后空重载：基线 stamp 落在 past（内容未变，无害）
        mem = make_mem(1, "redis queue depth")
        pairs.append((mem, rel_of(mem)))
        a.sync(mem, rel_of(mem))  # a（另一进程）写入并更新缓存文件
        assert b.candidates(tokenize("redis")) == [rel_of(mem)]  # b 检测 mtime 变化后重载


class TestOutOfBandInFirstNamespace:
    """跨 ns 带外新增的读路径自愈（回归钉：活性协议复制漂移出的真 bug）。

    旧实现的 _dirs_newer_than 循环错位：type_dirs 在 ns 循环内赋值、循环外
    消费，只有最后一个 ns 的 type 目录被检查——排序靠前的 ns 里发生带外
    新增时 reconcile 不触发，新记忆永久检索不到。修复后判定收拢 liveness
    单点（语义在 test_liveness 钉死），这里钉的是 Index._ensure_fresh 的接线。
    """

    def test_out_of_band_add_in_first_ns_is_reconciled(self, index, tmp_path: Path):
        idx, pairs = index
        old_mem = make_mem(1, "redis queue depth")
        idx.sync(old_mem, rel_of(old_mem))
        stamp = cache_file(tmp_path).stat().st_mtime_ns
        # ns_a 先建（贴合"目录创建序即遍历序"的文件系统形态，红态更敏感）
        second_type = tmp_path / "namespaces" / "ns_b" / "fact"
        first_type = tmp_path / "namespaces" / "ns_a" / "episode"
        second_type.mkdir(parents=True)
        first_type.mkdir(parents=True)
        older, newer = stamp - 1_000_000, stamp + 1_000_000
        for d in (tmp_path / "namespaces", first_type.parent, second_type.parent, second_type):
            os.utime(d, ns=(older, older))
        os.utime(first_type, ns=(newer, newer))
        hand = make_mem(2, "zabbix queue alerts")
        rel = "namespaces/ns_a/episode/20260101_000002.md"
        pairs.append((hand, rel))
        assert idx.candidates(tokenize("zabbix")) == [rel]


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
        return {tok: set(rels) for tok, rels in raw["index"].items()}

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


class TestTokenStatsCache:
    """tokens.json v2 的 per-doc token 统计（#41）：检索候选路径免 parse 的数据源。

    钉四件事：doc_entries 的形状（tf 频表/len/先验）；旧版 v1 缓存视作死
    缓存走重建（兼容语义）；对账 diff 覆盖「先验-only 变更」（跨进程
    feedback 只动 uses/last_used，也必须被对账捕获）；条目与倒排一起
    被 purge（删除不残留）。
    """

    def test_doc_entries_shape(self, index):
        idx, _ = index
        mem = make_mem(1, "redis queue depth redis")
        idx.sync(mem, rel_of(mem))
        (entry,) = idx.doc_entries([rel_of(mem)]).values()
        assert entry["tf"] == {"redis": 2, "queue": 1, "depth": 1}
        assert entry["len"] == 4
        assert entry["id"] == mem.id
        assert entry["ns"] == "_shared" and entry["type"] == "episode"
        assert entry["confidence"] == 0.5 and entry["uses"] == 0
        assert entry["last_used"] is None and entry["valid_until"] is None

    def test_doc_entries_missing_rel_absent(self, index):
        """不在索引里的 rel（向量路独有召回）不出现——调用方回退 parse。"""
        idx, _ = index
        assert idx.doc_entries(["namespaces/_shared/fact/nope.md"]) == {}

    def test_legacy_v1_cache_rebuilds_on_access(self, index, tmp_path: Path):
        """旧版纯倒排 schema（无 v/docs 包装）视作死缓存：下一次访问全量重建为当前版本。"""
        idx, pairs = index
        mem = make_mem(1, "redis queue depth")
        pairs.append((mem, rel_of(mem)))
        idx._dir.mkdir(parents=True, exist_ok=True)
        cache_file(tmp_path).write_text(json.dumps({"redis": [rel_of(mem)]}), encoding="utf-8")
        assert idx.candidates(tokenize("redis")) == [rel_of(mem)]
        data = json.loads(cache_file(tmp_path).read_text(encoding="utf-8"))
        assert data["v"] == CACHE_VERSION
        assert idx.doc_entries([rel_of(mem)])[rel_of(mem)]["id"] == mem.id

    def test_legacy_v2_cache_without_project_rebuilds(self, index, tmp_path: Path):
        """v2 缓存（#41 键集，条目无 project 字段）在 v3 下视作死缓存：全量重建后
        条目携带 project——检索快路径的 fail-closed 依赖条目键集完整（ADR 0010）。"""
        idx, pairs = index
        mem = make_mem(1, "redis queue depth")
        pairs.append((mem, rel_of(mem)))
        idx._dir.mkdir(parents=True, exist_ok=True)
        v2_entry = {
            "tf": {"redis": 1},
            "len": 3,
            "id": mem.id,
            "ns": mem.ns,
            "type": mem.type,
            "source": mem.source,
            "confidence": mem.confidence,
            "uses": 0,
            "created": mem.created,
            "last_used": None,
            "valid_until": None,
        }
        cache_file(tmp_path).write_text(
            json.dumps({"v": 2, "index": {"redis": [rel_of(mem)]}, "docs": {rel_of(mem): v2_entry}}),
            encoding="utf-8",
        )
        assert idx.candidates(tokenize("redis")) == [rel_of(mem)]
        data = json.loads(cache_file(tmp_path).read_text(encoding="utf-8"))
        assert data["v"] == CACHE_VERSION
        assert "project" in idx.doc_entries([rel_of(mem)])[rel_of(mem)]

    def test_reconcile_updates_prior_only_change(self, index, tmp_path: Path):
        """对账的 diff 基准是整条 entry（tf + 先验）：tokens 不变、uses/last_used
        变了（跨进程 feedback 的形态）也要落进缓存——先验入了缓存（#41），
        漏更会让检索 emit 旧 uses。"""
        idx, pairs = index
        mem = make_mem(1, "redis queue depth")
        idx.sync(mem, rel_of(mem))
        assert idx.doc_entries([rel_of(mem)])[rel_of(mem)]["uses"] == 0
        mem.uses, mem.last_used = 3, "2026-09-01"
        pairs.clear()
        pairs.append((mem, rel_of(mem)))
        # 触发对账：压缓存 mtime 到过去 + bump ns 目录 mtime（带外变更形态）
        past = time.time() - 60
        os.utime(cache_file(tmp_path), (past, past))
        ns_dir = tmp_path / "namespaces" / "_shared" / "episode"
        ns_dir.mkdir(parents=True, exist_ok=True)
        newer = int(time.time() * 1e9) + 60_000_000_000
        os.utime(ns_dir, ns=(newer, newer))
        entry = idx.doc_entries([rel_of(mem)])[rel_of(mem)]
        assert (entry["uses"], entry["last_used"]) == (3, "2026-09-01")

    def test_purge_drops_docs_entry_too(self, index, tmp_path: Path):
        """归档/删除清除倒排的同时清 docs 条目——残留会让已删记忆继续当候选。"""
        idx, _ = index
        mem = make_mem(1, "redis queue depth")
        idx.sync(mem, rel_of(mem))
        mem.archived = True
        idx.sync(mem, rel_of(mem))
        data = json.loads(cache_file(tmp_path).read_text(encoding="utf-8"))
        assert rel_of(mem) not in data["docs"]
        assert idx.doc_entries([rel_of(mem)]) == {}

    def test_rebuild_skips_archived_pairs(self, index):
        """活动区里 archived 标记的序对不进缓存：读路径经缓存条目无从复查
        archived，跳过与查询侧过滤等价（归档不可见语义保持）。"""
        idx, _ = index
        live = make_mem(1, "redis queue depth")
        dead = make_mem(2, "docker network bridge", archived=True)
        idx.rebuild([(live, rel_of(live)), (dead, rel_of(dead))])
        assert idx.candidates(tokenize("docker")) == []
        assert idx.doc_entries([rel_of(dead)]) == {}


class TestTokenStatsSearchParity:
    """检索快慢路径奇偶性（#41 token stats）：缓存条目路径与强制 parse 回退
    路径的 search 输出必须逐位一致——「检索结果逐位不变」红线的 store 级
    characterization（契约 test_search 钉五要素，这里钉缓存切换本身）。

    强制回退的手法：Index.doc_entries 打补丁返回空——scored_candidates 对
    全部候选走 parse 分支（即 #41 之前的旧路径）。
    """

    def _seed(self, store: MemoryStore) -> None:
        store.write(content="redis cache eviction policy", type="fact", source="agent-a", confidence=0.8)
        store.write(content="redis persistence aof rdb 取舍", type="insight", source="agent-a", confidence=0.4)
        store.write(content="docker network bridge mode", type="episode", source="agent-a")
        # 手编文件：未加引号 ISO 日期（YAML 解析成 date 对象）——缓存条目的
        # 日期安全化必须与 parse 路径的坏日期语义逐位对齐
        hand = store.ns_root / "_shared" / "fact" / "20260101_handdate.md"
        hand.write_text(
            "---\nid: 20260101_handdate\nns: _shared\ntype: fact\n"
            "source: agent-x\ncreated: 2026-09-01\nlast_used: 2026-09-20\n---\n\nredis 部署 配置 要点\n",
            encoding="utf-8",
        )

    def test_search_cache_path_matches_parse_fallback(self, store: MemoryStore, monkeypatch):
        self._seed(store)
        for query in ("redis 部署", "redis", "配置", "docker network"):
            fast = store.search(query, top_k=10)
            monkeypatch.setattr(Index, "doc_entries", lambda self, rels: {})
            try:
                slow = store.search(query, top_k=10)
            finally:
                monkeypatch.undo()
            assert fast == slow, f"cache/parse divergence for query: {query!r}"

    def test_search_after_rebuild_matches_incremental(self, store: MemoryStore):
        """缓存自愈三形态（写路径 sync / 对账 / 显式 rebuild）产出等价检索：
        rebuild 重建的 docs 条目与增量路径写的逐位一致。"""
        self._seed(store)
        incremental = store.search("redis 部署", top_k=10)
        store.rebuild_index()
        assert store.search("redis 部署", top_k=10) == incremental
