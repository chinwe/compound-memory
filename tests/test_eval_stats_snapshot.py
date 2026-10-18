"""eval_stats_snapshot 笔记脱敏——快照产物会入库，不得携带本机绝对路径。

隐私门禁（check-privacy.sh）拦 /Users/<user> 等禁串；脚本 build_note 曾把执行
命令（含仓库绝对路径与 --root 值）原样写进 .md，导致真实用户名环境下每次
快照都要人工脱敏才能提交。这里钉住：笔记里的命令与 stderr 必须净化，而
实际执行的命令不受影响；--root 值只保留末段目录名（库仍可辨识）。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "eval_stats_snapshot.py"
_spec = importlib.util.spec_from_file_location("eval_stats_snapshot", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

REPO = str(mod.REPO)
HOME = str(Path.home())


class TestDisplayCommand:
    def test_repo_prefix_becomes_placeholder(self):
        out = mod.display_command(["uv", "run", "--project", REPO, "compound-memory", "stats"])
        assert "<repo>" in out
        assert not any(REPO in token for token in out)

    def test_repo_child_path_becomes_placeholder_child(self):
        out = mod.display_command([f"{REPO}/.venv/bin/compound-memory"])
        assert out == ["<repo>/.venv/bin/compound-memory"]

    def test_home_prefix_becomes_tilde(self):
        out = mod.display_command([f"{HOME}/.local/bin/uv"])
        assert out == ["~/.local/bin/uv"]

    def test_root_value_keeps_only_basename(self):
        root = f"{HOME}/.test-tmp/compound-memory-eval-f10s-b"
        out = mod.display_command(["cm", "--root", root, "stats"])
        assert "--root" in out
        assert "compound-memory-eval-f10s-b" in out
        assert HOME not in " ".join(out)


class TestBuildNote:
    def test_note_carries_no_local_absolute_paths(self):
        command = [
            "uv", "run", "--project", REPO,
            "compound-memory", "--root", f"{HOME}/x/eval-root", "stats",
        ]
        note = mod.build_note(
            stamp="T", command=command, exit_code=0, stderr="",
            json_name="s.json", stats_ok=True,
        )
        assert REPO not in note
        assert HOME not in note
        assert "<repo>" in note
        assert "eval-root" in note

    def test_stderr_is_sanitized_too(self):
        note = mod.build_note(
            stamp="T", command=["cm"], exit_code=1,
            stderr=f"error at {REPO}/x", json_name="s.json", stats_ok=True,
        )
        assert REPO not in note
        assert "<repo>/x" in note


class TestMainEndToEnd:
    def test_generated_note_is_sanitized(self, tmp_path: Path):
        fake = tmp_path / "fakecm"
        fake.write_text('#!/bin/sh\necho \'{"ok": true}\'\n')
        fake.chmod(0o755)
        outdir = tmp_path / "out"
        root = tmp_path / "eval-root"
        rc = mod.main(["--cli", str(fake), "--root", str(root), "--out-dir", str(outdir)])
        assert rc == 0
        notes = list(outdir.glob("*-stats.md"))
        assert len(notes) == 1
        text = notes[0].read_text(encoding="utf-8")
        assert str(tmp_path) not in text
        assert "eval-root" in text
