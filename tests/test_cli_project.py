"""project 作用域的 CLI adapter 缝（ADR 0010）：--project 显式参数与
COMPOUND_MEMORY_PROJECT env 回退。

env 只在 adapter 层读（_caller_project），store 自身不读环境变量——与
COMPOUND_MEMORY_AGENT_ID 同型，保测试与库调用的确定性。空串 env 视为
未携带（调用方没声明项目 = 全局会话，而不是撞 slug 校验报错）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from compound_memory import cli as cli_mod
from compound_memory.storage import MemoryStore

from conftest import CLOCK_DATE, sandbox_safe_remove


def _run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, Any]:
    """端到端跑 main()：拦截 auto_encoder（不依赖本机 vec 模型），返回 (rc, JSON 输出)。"""
    monkeypatch.setattr(cli_mod, "auto_encoder", lambda: None)
    rc = cli_mod.main(list(argv))
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


class TestCliProjectEnvFallback:
    def test_write_without_flag_or_env_stays_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        monkeypatch.delenv("COMPOUND_MEMORY_PROJECT", raising=False)
        root = tmp_path / "memroot"
        rc, out = _run(
            monkeypatch, capsys, "--root", str(root), "write", "cli project marker", "fact", "agent-a"
        )
        assert rc == 0 and out["project"] is None

    def test_env_fallback_tags_write_and_scopes_search(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """env 回退（adapter 层读）：write 落 project；search 不带 --project 时经同一
        env 缺省声明项目 → 见 全局 ∪ 该项目（fail-closed 的缺省在此被 env 覆写）。"""
        root = tmp_path / "memroot"
        monkeypatch.setenv("COMPOUND_MEMORY_PROJECT", "agenthub")
        rc, out = _run(
            monkeypatch, capsys, "--root", str(root), "write", "cli project marker", "fact", "agent-a"
        )
        assert rc == 0 and out["project"] == "agenthub"
        store = MemoryStore(root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove)
        glob = store.write("cli global marker", type="fact", source="agent-a")
        rc, hits = _run(monkeypatch, capsys, "--root", str(root), "search", "cli marker")
        assert rc == 0
        assert {h["id"] for h in hits} == {out["id"], glob["id"]}

    def test_flag_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = tmp_path / "memroot"
        monkeypatch.setenv("COMPOUND_MEMORY_PROJECT", "agenthub")
        rc, out = _run(
            monkeypatch, capsys,
            "--root", str(root), "write", "cli project marker", "fact", "agent-a",
            "--project", "webmail",
        )
        assert rc == 0 and out["project"] == "webmail"

    def test_empty_env_string_means_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        """空串 env = 调用方没声明项目：归一为 None，不是 slug 校验错误。"""
        root = tmp_path / "memroot"
        monkeypatch.setenv("COMPOUND_MEMORY_PROJECT", "")
        rc, out = _run(
            monkeypatch, capsys, "--root", str(root), "write", "cli project marker", "fact", "agent-a"
        )
        assert rc == 0 and out["project"] is None
