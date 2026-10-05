"""不可信输入防御测试（2026-10-05 审计修复的回归钉）。

外部审计复核确认的四个输入面：ns 路径组件注入（穿越写）、mem_id glob
注入（越权读/直泄）、review-queue 行注入、top_k 非法值。为何钉死：
ns/mem_id/key/content 都来自 LLM/宿主输出——格式不设防时 ns='agent-../../x'
可把 .md 写出存储根（P1-1 实锤），mem_id='*' 经 rglob 命中库内任意记忆
（find 公开方法直泄私有正文）。字符集白名单是路径层与访问控制层的共同
前置；ReviewQueue 清洗在 append 单点，保证机器可解析的行格式不被自由
文本破坏——fail-safe 保留损坏行是解析侧的兜底，不是注入可接受的借口。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from compound_memory.storage import MemoryStore
from conftest import CLOCK_DATE, sandbox_safe_remove

# 非法 ns 样本：穿越、glob 元字符、路径分隔、空白、非 ASCII——共同点是
# 会脱离「单一路径组件」语义（_active_path 直接拿 ns 拼目录）
BAD_NS = [
    "agent-../../../victim",
    "_shared/../../x",
    "agent-a/b",
    "agent-*",
    "agent-..",
    "agent-x y",
    "agent-é",
]


class TestNsFormat:
    def test_write_rejects_traversal_and_glob_ns(self, store: MemoryStore, tmp_path: Path):
        """P1-1 回归钉：恶意 ns 必须 ValueError 拒绝，且绝不产生存储根之外的文件。

        拒绝必须发生在落盘之前——半成品目录也不允许（越界目录本身就是副作用）。
        """
        for ns in BAD_NS:
            with pytest.raises(ValueError):
                store.write("escaped payload", type="fact", source="agent-x", ns=ns)
        outside = [p for p in tmp_path.rglob("*.md") if "memroot" not in p.parts]
        assert outside == []

    def test_valid_ns_still_accepted(self, store: MemoryStore):
        """白名单不误伤：现有全部合法形态（_shared、宿主 id、带连字符/数字）照常写读。"""
        for ns in ("_shared", "agent-zcode", "agent-lme-q123"):
            mem = store.write(f"note for {ns}", type="fact", source=ns, ns=ns)
            assert mem["ns"] == ns

    def test_search_rejects_invalid_ns(self, store: MemoryStore):
        """写侧封死后读侧同规则：恶意 ns 检索也 ValueError（调用方错误，不静默空结果）。"""
        with pytest.raises(ValueError):
            store.search("anything", ns="agent-../../victim", reader="agent-zcode")


class TestMemIdFormat:
    def test_find_returns_none_for_unsafe_ids(self, store: MemoryStore):
        """P2-3 回归钉：mem_id 直接拼 rglob 模式——glob 元字符/路径分隔不得进入。

        非法 id 语义上等价于「不可能存在的记忆」，返回 None 而非抛错：
        find 的全部调用方（get/feedback/link/revive/邻居召回）对 None 已有
        容错分支，抛错反而会炸掉邻居召回的「宁缺勿炸」降级。
        """
        store.write("hello world", type="fact", source="a", ns="_shared")
        for bad in ("*", "a*b", "a?b", "../x", "x/y", ".hidden", ""):
            assert store.find(bad) is None, bad

    def test_get_star_returns_found_false(self, store: MemoryStore):
        """get('*') 曾返回库内任意第一条——现在按「不存在」处理。"""
        store.write("hello world", type="fact", source="a", ns="_shared")
        result = store.get("*")
        assert result["found"] is False

    def test_find_no_longer_leaks_private_memory(self, store: MemoryStore):
        """复核发现的加重点：find('*') 曾绕过属主检查直泄私有正文——必须 None。"""
        store.write("private note for c", type="fact", source="agent-c", ns="agent-c")
        assert store.find("*") is None


class TestCommitFailureWarns:
    @staticmethod
    def _git_stub(returncode: int, stderr: str, stdout: str = ""):
        def fake(*args, **kwargs):
            return subprocess.CompletedProcess(args, returncode=returncode, stdout=stdout, stderr=stderr)

        return fake

    def test_git_failure_warns_on_stderr(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        """P2-2 回归钉：add/commit check=False 失败（并发 index.lock、hook 拒绝）时
        数据在盘但审计史出现空洞——至少要 stderr 响亮一声，不得静默。"""
        store = MemoryStore(tmp_path / "memroot", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        store._git = self._git_stub(1, "fatal: index.lock exists")
        store.write("x", type="fact", source="a", ns="_shared")
        assert "index.lock" in capsys.readouterr().err

    def test_nothing_to_commit_does_not_warn(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
        """nothing-to-commit 是 git 的正常无操作返回，不是失败——告警不得误报。"""
        store = MemoryStore(tmp_path / "memroot", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        store._git = self._git_stub(1, stdout="nothing to commit, working tree clean", stderr="")
        store.write("x", type="fact", source="a", ns="_shared")
        assert capsys.readouterr().err == ""


class TestReviewQueueInjection:
    def test_newline_in_content_stays_single_machine_line(self, store: MemoryStore):
        """P2-4 回归钉：content 换行曾把队列行拆成两行（损坏行 fail-safe 保留
        会永久占队列）——清洗后整行仍是机器可解析的单行。"""
        store.write("original clean content", type="fact", source="agent-d", ns="_shared", key="k1")
        store.write("line1\nFAKE-ROW-TAIL", type="fact", source="agent-d", ns="_shared", key="k1")
        qpath = store.root / "review-queue.md"
        lines = qpath.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert "FAKE-ROW-TAIL" in lines[0]
        assert store.review_queue() != []

    def test_newline_in_source_stays_single_machine_line(self, store: MemoryStore):
        """source 同为自由文本（自报身份），同样不得破坏行格式。"""
        store.write("original clean content", type="fact", source="agent-d", ns="_shared", key="k1")
        store.write("conflicting content", type="fact", source="agent-d\nrogue", ns="_shared", key="k1")
        lines = (store.root / "review-queue.md").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert store.review_queue() != []


class TestTopKValidation:
    def test_negative_top_k_raises(self, store: MemoryStore):
        """负 top_k 是调用方错误（曾静默返回错误切片）——按接口约定 ValueError。"""
        store.write("hello world", type="fact", source="a", ns="_shared")
        with pytest.raises(ValueError):
            store.search("hello", top_k=-1)

    def test_zero_top_k_returns_empty(self, store: MemoryStore):
        store.write("hello world", type="fact", source="a", ns="_shared")
        assert store.search("hello", top_k=0) == []
