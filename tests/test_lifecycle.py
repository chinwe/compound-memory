"""Tests at the CLI / store seam: decay, archive, revive, index cache, git, stats.

MCP 工具行为在 test_mcp_tools.py（MCP tool 边界缝）覆盖；
本文件覆盖不经过 MCP 暴露的运维面：decay_sweep / revive / rebuild_index / stats / git。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from compound_memory.cli import main as cli_main
from compound_memory.storage import MemoryStore
from compound_memory.storage.stats import _conf_bucket, _uses_bucket
from conftest import CLOCK_DATE, sandbox_safe_remove


def _days_ago(n: int) -> str:
    """相对测试固定"今天"（CLOCK_DATE）推算——store 的 clock 已注入同一日期。"""
    return (CLOCK_DATE - dt.timedelta(days=n)).isoformat()


class TestDecayAndArchive:
    def test_stale_low_use_episode_gets_archived(self, store: MemoryStore):
        stale = store.write(content="旧的部署经验", type="episode", source="agent-a", created=_days_ago(120))
        fresh = store.write(content="新的部署经验", type="episode", source="agent-a")
        archived = store.decay_sweep()
        assert stale["id"] in archived
        assert fresh["id"] not in archived

    def test_heavily_used_episode_survives_ttl(self, store: MemoryStore):
        used = store.write(content="高频使用的经验", type="episode", source="agent-a", created=_days_ago(120))
        for agent in ("a", "b", "c"):
            store.feedback(used["id"], agent)
        archived = store.decay_sweep()
        assert used["id"] not in archived

    def test_fact_never_archived(self, store: MemoryStore):
        fact = store.write(content="长期事实", type="fact", source="agent-a", key="env", created=_days_ago(400))
        assert store.decay_sweep() == []

    def test_archived_memory_still_gettable_but_not_searchable(self, store: MemoryStore):
        mem = store.write(content="Vercel 超时限制是 10 秒", type="episode", source="agent-a", created=_days_ago(120))
        store.decay_sweep()
        got = store.get(mem["id"])
        assert got["found"] is True
        assert got["archived"] is True
        assert store.search("Vercel 超时") == []

    def test_recently_used_survives_despite_old_created(self, store: MemoryStore):
        """spec: 衰减看'长期未用'——last_used 新则不归档."""
        mem = store.write(content="最近用过的老经验", type="episode", source="agent-a", created=_days_ago(120))
        store.feedback(mem["id"], "agent-a")  # last_used = today
        assert store.decay_sweep() == []

    def test_revive_restores_searchability(self, store: MemoryStore):
        mem = store.write(content="Next.js 静态导出经验", type="episode", source="agent-a", created=_days_ago(120))
        store.decay_sweep()
        revived = store.revive(mem["id"])
        assert revived["archived"] is False
        hits = store.search("Next.js 静态导出")
        assert [h["id"] for h in hits] == [mem["id"]]

    def test_feedback_on_archived_revives_it(self, store: MemoryStore):
        mem = store.write(content="被再次用到的旧经验", type="episode", source="agent-a", created=_days_ago(120))
        store.decay_sweep()
        result = store.feedback(mem["id"], "agent-b")
        assert result["archived"] is False
        assert result["uses"] == 1


    def test_bad_last_used_date_skipped_not_fatal(self, store: MemoryStore):
        """手编坏日期（spec story 10 鼓励直接编辑文件）不得杀掉整场 decay 扫描：
        坏日期的记忆跳过（宁可不归档，不因坏数据丢记忆），其余记忆照常处理。"""
        bad = store.write(content="被手编坏日期的记忆", type="episode", source="agent-a", created=_days_ago(120))
        path = store.ns_root / "_shared" / "episode" / f"{bad['id']}.md"
        text = path.read_text(encoding="utf-8").replace("created:", "last_used: not-a-date\ncreated:", 1)
        path.write_text(text, encoding="utf-8")
        good = store.write(content="正常的陈旧记忆", type="episode", source="agent-a", created=_days_ago(120))
        archived = store.decay_sweep()
        assert bad["id"] not in archived
        assert good["id"] in archived


class TestIndexCache:
    def test_rebuild_index_reports_counts_and_keeps_search(self, store: MemoryStore):
        """显式重建动词（运维面）：报告计数，且重建后检索如常。"""
        a = store.write(content="Python GIL 基础知识", type="episode", source="agent-a")
        store.write(content="Go goroutine 并发知识", type="episode", source="agent-a")
        counts = store.rebuild_index()
        assert counts["memories"] == 2
        assert [h["id"] for h in store.search("Python GIL")] == [a["id"]]


class TestRemovalAdapter:
    def test_default_remover_unlinks_in_production(self, tmp_path: Path):
        """生产默认 adapter 是真删除：单文件 unlink 不受沙箱批量守卫影响，
        归档/复活不在生产库留 .rm 尸体（改名式 remover 只存在于测试侧）。"""
        store = MemoryStore(tmp_path / "prodroot")
        mem = store.write(content="将被归档的记忆", type="episode", source="agent-a", created=_days_ago(120))
        store.decay_sweep()
        active_dir = store.ns_root / "_shared" / "episode"
        assert not (active_dir / f"{mem['id']}.md").exists()
        assert not list(active_dir.glob("*.rm"))


class TestGit:
    def test_write_and_feedback_create_commits(self, store: MemoryStore):
        mem = store.write(content="git 测试记忆", type="episode", source="agent-a")
        log1 = store.git_log(20)
        assert any("write" in line for line in log1)
        store.feedback(mem["id"], "agent-b")
        log2 = store.git_log(20)
        assert any("feedback" in line for line in log2)
        assert len(log2) > len(log1)

    def test_no_git_binary_disables_gracefully(self, tmp_path: Path):
        """launchd 最小 PATH 等无 git 环境：探测失败即降级——写读照常，不报错。"""
        store = MemoryStore(tmp_path / "nogit", git_probe=lambda: False)
        mem = store.write(content="无 git 环境", type="episode", source="agent-a")
        assert mem["id"]
        assert store.search("无 git") != []

    def test_init_commit_only_on_first_creation(self, tmp_path: Path):
        """首次创建产生 init commit；重开 store（CLI/MCP 每次启动都构造）不得把
        带外手编的文件吞进误导性的第二次 "init" 提交——带外变更由启动对账
        （_recover_orphan_changes）收编进明确标注的恢复提交：不声称作者、
        不错位归因到下一个写动词，init 提交历史不被伪造。"""
        root = tmp_path / "reopen"
        MemoryStore(root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        handmade = root / "namespaces" / "_shared" / "fact" / "20261001_handed.md"
        handmade.write_text(
            "---\nid: 20261001_handed\nns: _shared\ntype: fact\nsource: agent-zcode\n"
            "created: '2026-10-01'\n---\n\n带外手编的记忆内容\n",
            encoding="utf-8",
        )
        store2 = MemoryStore(root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        log = store2.git_log(50)
        assert sum("init compound-memory store" in line for line in log) == 1
        assert "orphan changes recovered" in log[0]
        assert store2._git("status", "--porcelain").stdout.strip() == ""


class TestStats:
    def test_stats_counts(self, store: MemoryStore):
        store.write(content="事实一", type="fact", source="agent-a", key="f1")
        store.write(content="经验一", type="episode", source="agent-a")
        store.write(content="私有草稿", type="episode", source="agent-tars", ns="agent-tars")
        stats = store.stats()
        assert stats["total"] == 3
        assert stats["by_type"]["episode"] == 2
        assert stats["by_ns"]["agent-tars"] == 1
        assert stats["review_queue_entries"] == 0

    def test_stats_health_extension(self, store: MemoryStore):
        """健康度扩展（story 20）：固定桶分布 + 复利活性两项 + 蒸馏产出量；
        既有键不动，新键只增不改。"""
        dead = store.write(content="死本金记忆", type="episode", source="agent-a")  # uses=0
        used = store.write(content="单 Agent 用过", type="episode", source="agent-a")
        store.feedback(used["id"], "agent-a")  # uses=1, conf=0.6
        cross = store.write(content="跨 Agent 验证", type="episode", source="agent-a")
        store.feedback(cross["id"], "agent-a")
        store.feedback(cross["id"], "agent-b")  # uses=2, conf=0.85, validated_by 2 人
        old_distilled = store.write(content="十天前的蒸馏产物", type="insight", source="agent-zcode",
                                     origin="distillation", created=_days_ago(10))
        fresh_distilled = store.write(content="今天的蒸馏产物", type="insight", source="agent-zcode",
                                      origin="distillation")
        stats = store.stats()
        assert stats["uses_histogram"] == {"0": 3, "1-2": 2, "3-5": 0, "6-9": 0, "10+": 0}
        assert stats["confidence_histogram"] == {"<0.3": 0, "0.3-0.6": 3, "0.6-0.8": 1, "0.8-1.0": 1}
        assert stats["recent_feedback_7d"] == 2  # used/cross 今天被 feedback；dead 从未用过
        assert stats["cross_validated"] == 1
        assert stats["distilled_total"] == 2
        assert stats["distilled_recent_7d"] == 1  # 老产物 created 超窗
        # 既有键不受影响
        assert stats["total"] == 5
        assert stats["by_type"]["insight"] == 2

    def test_stats_archived_distillation_still_counted(self, store: MemoryStore):
        """产出量统计覆盖归档区——蒸馏产出是历史事实，不因源/产物归档而消失。"""
        product = store.write(content="将被归档的产物", type="insight", source="agent-zcode", origin="distillation")
        store._archive(store.find(product["id"]))  # type: ignore[arg-type]
        stats = store.stats()
        assert stats["distilled_total"] == 1
        assert stats["distilled_recent_7d"] == 1

    def test_stats_bucket_boundaries(self):
        """桶边界单点验证：uses 以 3（归档存活线）分桶，confidence 以 0.3/0.6/0.8 分桶。"""
        assert [_uses_bucket(u) for u in (0, 1, 2, 3, 5, 6, 9, 10, 99)] == [
            "0", "1-2", "1-2", "3-5", "3-5", "6-9", "6-9", "10+", "10+"
        ]
        assert [_conf_bucket(c) for c in (0.0, 0.29, 0.3, 0.59, 0.6, 0.79, 0.8, 1.0)] == [
            "<0.3", "<0.3", "0.3-0.6", "0.3-0.6", "0.6-0.8", "0.6-0.8", "0.8-1.0", "0.8-1.0"
        ]


class TestReviewResolve:
    def _conflict(self, store: MemoryStore, key: str) -> tuple[str, str]:
        """制造一对同 key fact 冲突（内容不同才触发队列），返回 (old_id, new_id)。"""
        old = store.write(content=f"甲版本事实 {key}", type="fact", source="agent-a", key=key)
        new = store.write(content=f"乙版本事实 {key}", type="fact", source="agent-a", key=key)
        return old["id"], new["id"]

    def test_resolve_removes_matching_line_and_updates_stats(self, store: MemoryStore):
        """#14 验收：命中行清除 + stats.review_queue_entries 同步归零。"""
        old, new = self._conflict(store, "k1")
        assert len(store.review_queue()) == 1
        out = store.review_resolve([old])
        assert out == {
            "resolved": 1,
            "remaining": 0,
            "rows": [{"old": old, "new": new}],
            "archived": [old],
        }
        assert store.review_queue() == []
        assert store.stats()["review_queue_entries"] == 0

    def test_resolve_keeps_unmatched_lines(self, store: MemoryStore):
        """多行队列只清命中行：new id 与 old id 任一命中均算涉及。"""
        old1, new1 = self._conflict(store, "k1")
        old2, new2 = self._conflict(store, "k2")
        out = store.review_resolve([old1, new2])
        assert out == {
            "resolved": 2,
            "remaining": 0,
            "rows": [{"old": old1, "new": new1}, {"old": old2, "new": new2}],
            "archived": [old1, new2],
        }

        old3, new3 = self._conflict(store, "k3")
        old4, new4 = self._conflict(store, "k4")
        out = store.review_resolve([new4])
        assert out == {
            "resolved": 1,
            "remaining": 1,
            "rows": [{"old": old4, "new": new4}],
            "archived": [new4],
        }

    def test_resolve_atomic_on_unknown_id(self, store: MemoryStore):
        """任一 id 未命中 ⇒ 整体拒绝、队列原样保留：登记是原子动作，不做半清。"""
        old, _ = self._conflict(store, "k1")
        with pytest.raises(ValueError, match="nope"):
            store.review_resolve([old, "nope"])
        assert len(store.review_queue()) == 1

    def test_resolve_requires_ids_or_all_exclusively(self, store: MemoryStore):
        with pytest.raises(ValueError, match="ids or --all"):
            store.review_resolve([])
        with pytest.raises(ValueError, match="either"):
            store.review_resolve(["x"], all=True)

    def test_resolve_all_clears_and_is_idempotent(self, store: MemoryStore):
        self._conflict(store, "k1")
        self._conflict(store, "k2")
        assert store.review_resolve(all=True) == {"resolved": 2, "remaining": 0, "archived": []}
        assert store.review_queue() == []
        assert store.review_resolve(all=True) == {"resolved": 0, "remaining": 0, "archived": []}

    def test_resolve_without_queue_file(self, store: MemoryStore):
        """队列文件尚不存在（无冲突史）：--all 幂等空转；按 id 是未命中错误。"""
        assert store.review_resolve(all=True) == {"resolved": 0, "remaining": 0, "archived": []}
        with pytest.raises(ValueError, match="not found"):
            store.review_resolve(["nope"])

    def test_resolve_creates_git_commit(self, store: MemoryStore):
        old, _ = self._conflict(store, "k1")
        before = store.git_log(50)
        store.review_resolve([old])
        after = store.git_log(50)
        assert len(after) > len(before)
        assert any("review resolve 1 entries" in line for line in after)

    def test_resolve_archives_dropped_side(self, store: MemoryStore):
        """裁决的废置方 = 调用方传入的 id：清行同时归档该条，对侧保留活动区。

        为何归档必须跟随清行动作：2026-10-05 运维实测，历次 review resolve
        只清行不归档，废置旧版（uses=0）全部滞留活动区，又被蒸馏候选的
        uses≥1 门槛滤出人审视野——同 key 多版本并存就是这么累积的。
        """
        old, new = self._conflict(store, "k1")
        out = store.review_resolve([old])
        assert out["archived"] == [old]
        assert store.get(old)["archived"] is True
        assert store.get(new)["archived"] is False
        assert store.stats()["archived"] == 1

    def test_resolve_all_clears_without_archiving(self, store: MemoryStore):
        """--all 只清行：队列行本身不表达裁决方向（old/new 任一可保留），
        自动归档需要调用方逐行指认——缺这个信息就不动手，不做方向推断。"""
        old, _ = self._conflict(store, "k1")
        out = store.review_resolve(all=True)
        assert out == {"resolved": 1, "remaining": 0, "archived": []}
        assert store.get(old)["archived"] is False


class TestCli:
    def test_cli_write_search_feedback_roundtrip(self, tmp_path: Path, capsys):
        root = tmp_path / "cliroot"
        assert cli_main(["--root", str(root), "write", "Rust 所有权规则", "episode", "agent-cli"]) == 0
        out = json.loads(capsys.readouterr().out)
        mem_id = out["id"]

        assert cli_main(["--root", str(root), "search", "Rust 所有权"]) == 0
        hits = json.loads(capsys.readouterr().out)
        assert [h["id"] for h in hits] == [mem_id]

        assert cli_main(["--root", str(root), "feedback", mem_id, "agent-cli"]) == 0
        got = json.loads(capsys.readouterr().out)
        assert got["uses"] == 1
        assert got["confidence"] == pytest.approx(0.6)

    def test_cli_rejects_bad_type(self, tmp_path: Path):
        with pytest.raises(SystemExit) as exc:
            cli_main(["--root", str(tmp_path / "r"), "write", "x", "bogus", "agent"])
        assert exc.value.code == 2

    def test_cli_permission_error_returns_json_not_traceback(self, tmp_path: Path, capsys):
        """接口错误约定的 CLI 侧：越权写 agent-* 命名空间必须翻译成 JSON + exit 2，
        而不是裸栈崩溃（旧实现只捕 ValueError，此路径直接 traceback）。"""
        code = cli_main(["--root", str(tmp_path / "p"), "write", "x", "episode", "agent-a", "--ns", "agent-tars"])
        assert code == 2
        err = json.loads(capsys.readouterr().err)
        assert "private" in err["error"]

    def test_cli_link_reports_missing_ids(self, tmp_path: Path, capsys):
        """按 id 动词的信封约定：不存在 = {"found": false}，不是异常也不是第三种键名。"""
        assert cli_main(["--root", str(tmp_path / "l"), "link", "nope", "alsono"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {"found": False, "missing": ["nope", "alsono"]}

    def test_cli_review_resolve_roundtrip(self, tmp_path: Path, capsys):
        """CLI 缝：制造同 key 冲突 → 按旧 id resolve → 清行 + 归档废置方。"""
        root = str(tmp_path / "rr")
        assert cli_main(["--root", root, "write", "甲版本事实", "fact", "agent-cli", "--key", "rk"]) == 0
        old = json.loads(capsys.readouterr().out)["id"]
        assert cli_main(["--root", root, "write", "乙版本事实不同内容", "fact", "agent-cli", "--key", "rk"]) == 0
        new = json.loads(capsys.readouterr().out)["id"]

        assert cli_main(["--root", root, "review-resolve", old]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out == {
            "resolved": 1,
            "remaining": 0,
            "rows": [{"old": old, "new": new}],
            "archived": [old],
        }

    def test_cli_review_resolve_unknown_id_returns_exit_2(self, tmp_path: Path, capsys):
        """接口错误约定的 CLI 侧翻译：未命中 id ⇒ stderr JSON + exit 2，不裸栈。"""
        code = cli_main(["--root", str(tmp_path / "rr2"), "review-resolve", "nope"])
        assert code == 2
        err = json.loads(capsys.readouterr().err)
        assert "not found" in err["error"]

    def test_cli_search_neighbors_toggle(self, tmp_path: Path, capsys):
        """search 默认内嵌邻居；--no-neighbors 关闭（hits 不带 neighbors 键）。"""
        root = str(tmp_path / "sroot")
        assert cli_main(["--root", root, "write", "锚点记忆内容", "episode", "agent-cli"]) == 0
        anchor = json.loads(capsys.readouterr().out)["id"]
        assert cli_main(["--root", root, "write", "外围邻居内容", "episode", "agent-cli"]) == 0
        side = json.loads(capsys.readouterr().out)["id"]
        assert cli_main(["--root", root, "link", anchor, side]) == 0
        capsys.readouterr()

        assert cli_main(["--root", root, "search", "锚点记忆"]) == 0
        hit = json.loads(capsys.readouterr().out)[0]
        assert [n["id"] for n in hit["neighbors"]] == [side]

        assert cli_main(["--root", root, "search", "锚点记忆", "--no-neighbors"]) == 0
        hit = json.loads(capsys.readouterr().out)[0]
        assert "neighbors" not in hit


class TestValidityWindow:
    """时态事实（valid_from/valid_until）：过期事实从检索与邻居召回中排除，但不消失——
    get 恒可读（"归档不丢数据"哲学的镜像）；"新事实是否取代旧事实"的裁决仍走 review 队列，
    有效期只决定检索可见性，绝不绕过冲突裁决。"""

    def test_expired_fact_excluded_from_search_but_gettable(self, store: MemoryStore):
        mem = store.write(
            content="项目 X 的负责人是张三", type="fact", source="agent-a", key="lead-x",
            valid_from="2026-01-01", valid_until=_days_ago(1),
        )
        assert store.search("项目 X 负责人") == []
        got = store.get(mem["id"])
        assert got["found"] is True
        assert got["valid_from"] == "2026-01-01"
        assert got["valid_until"] == _days_ago(1)

    def test_fact_valid_until_today_still_searchable(self, store: MemoryStore):
        """valid_until 含当日（"有效期至"）：当天仍可信。"""
        mem = store.write(content="V2 发布窗口是本周五", type="fact", source="agent-a",
                          valid_until=CLOCK_DATE.isoformat())
        assert [h["id"] for h in store.search("V2 发布窗口")] == [mem["id"]]

    def test_expired_fact_excluded_from_vector_recall_too(self, vec_store: MemoryStore):
        """候选并集两侧同一套活性语义：向量路不得给过期记忆留旁路。"""
        expired = vec_store.write(content="旧方案使用 A 服务", type="fact", source="agent-a",
                                  valid_until=_days_ago(1))
        alive = vec_store.write(content="新方案使用 B 服务", type="fact", source="agent-a")
        hits = vec_store.search("方案使用", top_k=10)
        assert expired["id"] not in [h["id"] for h in hits]
        assert alive["id"] in [h["id"] for h in hits]

    def test_expired_fact_not_recalled_as_neighbor(self, store: MemoryStore):
        old = store.write(content="旧接口返回字段 foo", type="fact", source="agent-a", valid_until=_days_ago(1))
        new = store.write(content="新接口返回字段 bar", type="fact", source="agent-a")
        store.link(old["id"], new["id"])
        (hit,) = store.search("新接口返回字段")
        assert hit["id"] == new["id"]
        assert hit["neighbors"] == []

    def test_write_rejects_bad_validity_dates(self, store: MemoryStore):
        """坏格式/逻辑矛盾响亮抛 ValueError（调用方错误），不静默落盘脏标注。"""
        with pytest.raises(ValueError, match="valid_until"):
            store.write(content="x", type="fact", source="agent-a", valid_until="10/01/2026")
        with pytest.raises(ValueError, match="after valid_until"):
            store.write(content="x", type="fact", source="agent-a",
                        valid_from="2026-12-31", valid_until="2026-01-01")

    def test_replacement_fact_still_goes_to_review_queue(self, store: MemoryStore):
        """时态字段不绕过冲突裁决：同 key 不同内容的新事实照旧进 review 队列——
        过期只管检索可见性，"旧的标注过期还是归档"由裁决方决定。"""
        store.write(content="负责人是张三", type="fact", source="agent-a", key="lead")
        new = store.write(content="负责人是李四", type="fact", source="agent-a", key="lead",
                          valid_until="2027-12-31")
        assert new["conflict"] is True
        assert len(store.review_queue()) == 1

    def test_expired_fact_skipped_by_distill_plan(self, store: MemoryStore):
        """过期事实不该被蒸馏固化进新产物——蒸馏扫过期候选同归档区一样排除。"""
        expired = store.write(content="待替换的方案 A 细节", type="insight", source="agent-a",
                              created=_days_ago(5), valid_until=_days_ago(1))
        plan = store.distill_plan(window_days=30)
        assert expired["id"] not in [c["id"] for c in plan["candidates"]]

    def test_stats_counts_expired_active(self, store: MemoryStore):
        store.write(content="过期事实待清理", type="fact", source="agent-a", valid_until=_days_ago(1))
        store.write(content="正常事实", type="fact", source="agent-a")
        assert store.stats()["expired_active"] == 1

    def test_cli_write_validity_roundtrip(self, tmp_path: Path, capsys):
        """CLI 缝：--valid-from/--valid-until 落盘 + get 读回。"""
        root = str(tmp_path / "vroot")
        assert cli_main(["--root", root, "write", "缓存清理窗口", "fact", "agent-cli",
                         "--valid-from", "2026-09-01", "--valid-until", "2026-09-30"]) == 0
        mem_id = json.loads(capsys.readouterr().out)["id"]
        assert cli_main(["--root", root, "get", mem_id]) == 0
        got = json.loads(capsys.readouterr().out)
        assert got["valid_from"] == "2026-09-01"
        assert got["valid_until"] == "2026-09-30"
