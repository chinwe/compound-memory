"""CLI maintain：一条运维命令按固定顺序跑 dream → 可选 decay → health-snapshot。

默认不衰减。--with-decay、RUN_DECAY=1、DECAY=1 任一才跑 decay，且只跑一次。
不写 dream-proposals，不调用 dream-apply / distill-apply。
任一步失败非 0，stderr 点名该步，后续步骤不跑。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from compound_memory import cli as cli_mod
from compound_memory.cli import build_parser
from compound_memory.storage import MemoryStore
from compound_memory.storage.dream import LAST_PLAN_FILENAME
from compound_memory.storage.locking import WriteLockError

from conftest import CLOCK_DATE, sandbox_safe_remove


def _run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, dict | None, str]:
    monkeypatch.setattr(cli_mod, "auto_encoder", lambda: None)
    rc = cli_mod.main(list(argv))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return rc, payload, captured.err


def _maintain_messages(err: str) -> list[str]:
    """stderr 里 maintain 步骤行，去掉时间戳，保留顺序。"""
    messages: list[str] = []
    for line in err.splitlines():
        marker = "maintain: "
        if marker in line:
            messages.append(line.split(marker, 1)[1])
    return messages


def _init_root(tmp_path: Path) -> Path:
    root = tmp_path / "memroot"
    MemoryStore(root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove, embedder=None)
    return root


def _seed_stale(root: Path) -> str:
    store = MemoryStore(root, clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove, embedder=None)
    created = (CLOCK_DATE - dt.timedelta(days=120)).isoformat()
    mem = store.write(content="旧的部署经验", type="episode", source="agent-a", created=created)
    return mem["id"]


def _argv(root: Path, *extra: str) -> list[str]:
    return ["--root", str(root), "maintain", "--now", CLOCK_DATE.isoformat(), *extra]


@pytest.fixture(autouse=True)
def _clear_decay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RUN_DECAY", raising=False)
    monkeypatch.delenv("DECAY", raising=False)
    monkeypatch.delenv("COMPOUND_MEMORY_AGENT_ID", raising=False)


class TestMaintainDefaults:
    def test_dream_then_snapshot_skips_decay(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
        root = _init_root(tmp_path)
        stale_id = _seed_stale(root)
        calls = {"decay": 0}
        real = MemoryStore.decay_sweep

        def _counted(self: MemoryStore) -> list[str]:
            calls["decay"] += 1
            return real(self)

        monkeypatch.setattr(MemoryStore, "decay_sweep", _counted)
        rc, payload, err = _run(monkeypatch, capsys, *_argv(root))
        assert rc == 0, err
        assert calls["decay"] == 0
        assert payload is not None
        assert payload["ok"] is True
        assert payload["decay"] is False
        assert payload["steps"] == ["dream", "health-snapshot"]
        assert "archived" not in payload

        plan = json.loads((root / "distill" / LAST_PLAN_FILENAME).read_text(encoding="utf-8"))
        assert plan["dry_run"] is True
        assert plan["stats_before"] == plan["stats_after"]
        assert plan["stats_after"]["archived"] == 0
        assert not (root / "distill" / "dream-proposals.json").exists()

        lines = (root / "health" / "health.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["date"] == CLOCK_DATE.isoformat()
        # 没跑 decay：快照里这条仍在活动区
        assert record["stats"]["archived"] == 0
        got = MemoryStore(root, clock=lambda: CLOCK_DATE, embedder=None).get(stale_id)
        assert got["archived"] is False

        messages = _maintain_messages(err)
        assert messages[0] == "step=dream"
        assert any(m.startswith("step=dream ok ") for m in messages)
        assert "step=decay skipped" in messages
        assert any(m.startswith("step=health-snapshot ok ") for m in messages)
        assert messages[-1] == "done"
        # 顺序：dream 完成 → 跳过 decay → 快照
        assert messages.index("step=decay skipped") < messages.index("step=health-snapshot")
        dream_ok = next(i for i, m in enumerate(messages) if m.startswith("step=dream ok "))
        assert dream_ok < messages.index("step=decay skipped")


class TestMaintainDecaySwitch:
    def test_with_decay_archives_before_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_root(tmp_path)
        stale_id = _seed_stale(root)
        rc, payload, err = _run(monkeypatch, capsys, *_argv(root, "--with-decay"))
        assert rc == 0, err
        assert payload is not None
        assert payload["decay"] is True
        assert payload["steps"] == ["dream", "decay", "health-snapshot"]
        assert payload["archived"] == [stale_id]

        plan = json.loads((root / "distill" / "last-plan.json").read_text(encoding="utf-8"))
        # dream 在 decay 之前：报告里的 stats 还没看到归档
        assert plan["stats_before"]["archived"] == 0
        assert plan["stats_after"]["archived"] == 0
        record = json.loads((root / "health" / "health.jsonl").read_text(encoding="utf-8").splitlines()[-1])
        assert record["stats"]["archived"] == 1
        got = MemoryStore(root, clock=lambda: CLOCK_DATE, embedder=None).get(stale_id)
        assert got["archived"] is True
        messages = _maintain_messages(err)
        assert "step=decay skipped" not in messages
        assert any(m.startswith("step=decay ok archived=1") for m in messages)
        decay_ok = next(i for i, m in enumerate(messages) if m.startswith("step=decay ok "))
        assert decay_ok < messages.index("step=health-snapshot")

    @pytest.mark.parametrize("env_name", ["RUN_DECAY", "DECAY"])
    def test_env_enables_decay_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        env_name: str,
    ):
        root = _init_root(tmp_path)
        stale_id = _seed_stale(root)
        calls = {"decay": 0}
        real = MemoryStore.decay_sweep

        def _counted(self: MemoryStore) -> list[str]:
            calls["decay"] += 1
            return real(self)

        monkeypatch.setattr(MemoryStore, "decay_sweep", _counted)
        monkeypatch.setenv(env_name, "1")
        rc, payload, _err = _run(monkeypatch, capsys, *_argv(root, "--with-decay"))
        assert rc == 0
        assert calls["decay"] == 1
        assert payload is not None
        assert payload["archived"] == [stale_id]

    def test_zero_env_stays_off(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_root(tmp_path)
        stale_id = _seed_stale(root)
        monkeypatch.setenv("RUN_DECAY", "0")
        monkeypatch.setenv("DECAY", "0")
        rc, payload, err = _run(monkeypatch, capsys, *_argv(root))
        assert rc == 0, err
        assert payload is not None
        assert payload["decay"] is False
        assert "step=decay skipped" in _maintain_messages(err)
        got = MemoryStore(root, clock=lambda: CLOCK_DATE, embedder=None).get(stale_id)
        assert got["archived"] is False


class TestMaintainFailures:
    def test_health_snapshot_failure_keeps_plan_and_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_root(tmp_path)

        def _boom(*_args: object, **_kwargs: object) -> dict:
            raise OSError("disk full")

        monkeypatch.setattr(cli_mod, "append_snapshot", _boom)
        rc, payload, err = _run(monkeypatch, capsys, *_argv(root))
        assert rc == 1
        assert payload is None
        messages = _maintain_messages(err)
        assert any(m.startswith("step=dream ok ") for m in messages)
        assert "step=decay skipped" in messages
        assert any(m.startswith("FAILED step=health-snapshot:") for m in messages)
        assert "done" not in messages
        assert (root / "distill" / "last-plan.json").is_file()
        assert not (root / "health" / "health.jsonl").exists()

    def test_decay_failure_skips_snapshot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_root(tmp_path)

        def _boom(self: MemoryStore) -> list[str]:
            raise WriteLockError("busy")

        monkeypatch.setattr(MemoryStore, "decay_sweep", _boom)
        rc, payload, err = _run(monkeypatch, capsys, *_argv(root, "--with-decay"))
        assert rc == 2
        assert payload is None
        messages = _maintain_messages(err)
        assert any(m.startswith("step=dream ok ") for m in messages)
        assert any(m.startswith("FAILED step=decay:") for m in messages)
        assert "step=health-snapshot" not in messages
        assert "done" not in messages
        assert (root / "distill" / "last-plan.json").is_file()
        assert not (root / "health" / "health.jsonl").exists()
        assert "busy" in err

    def test_bad_ns_fails_at_dream(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ):
        root = _init_root(tmp_path)
        rc, _payload, err = _run(monkeypatch, capsys, *_argv(root, "--ns", "../x"))
        assert rc == 2
        messages = _maintain_messages(err)
        assert any(m.startswith("FAILED step=dream:") for m in messages)
        assert "step=health-snapshot" not in messages
        assert not (root / "distill" / "last-plan.json").exists()
        assert not (root / "health" / "health.jsonl").exists()


class TestMaintainSurface:
    def test_help_documents_defaults_and_rejects_propose(self):
        action = next(
            item for item in build_parser()._actions if isinstance(item, argparse._SubParsersAction)
        )
        text = action.choices["maintain"].format_help()
        lowered = text.lower()
        assert "--with-decay" in text
        assert "dream-apply" in lowered
        assert "run_decay=1" in lowered
        assert "decay" in lowered
        assert "health-snapshot" in lowered
        parser = build_parser()
        _args, unknown = parser.parse_known_args(["maintain", "--propose"])
        assert unknown == ["--propose"]
        _args, unknown = parser.parse_known_args(["maintain", "--apply"])
        assert unknown == ["--apply"]


def _code(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


class TestDailyMaintainScript:
    def test_shell_only_execs_maintain(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        sh = (scripts / "daily-maintain.sh").read_text(encoding="utf-8")
        code = _code(sh)
        assert "set -eu" in code
        assert "compound-memory maintain" in code
        assert "dream" not in code
        assert "decay" not in code
        assert "health-snapshot" not in code
        assert "--propose" not in code
        assert "dream-apply" not in code
        assert "distill-apply" not in code
        # distill-prepare 仍是只跑 dream 的路径
        prepare = _code((scripts / "distill-prepare.sh").read_text(encoding="utf-8"))
        assert "dream --ns _shared" in prepare
        assert "maintain" not in prepare
        assert LAST_PLAN_FILENAME == "last-plan.json"
        assert f"distill/{LAST_PLAN_FILENAME}" in prepare

    @pytest.mark.skipif(
        not (shutil.which("uv") or (Path.home() / ".local" / "bin" / "uv").exists()),
        reason="uv unavailable (not on PATH, no ~/.local/bin/uv)",
    )
    def test_shell_smoke_writes_plan_and_snapshot(self, tmp_path: Path):
        repo = Path(__file__).resolve().parents[1]
        root = tmp_path / "script-root"
        uv = shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv")
        env = {**os.environ, "COMPOUND_MEMORY_ROOT": str(root), "UV_BIN": uv}
        env.pop("PYTHONPATH", None)
        env.pop("RUN_DECAY", None)
        env.pop("DECAY", None)
        proc = subprocess.run(
            ["sh", str(repo / "scripts" / "daily-maintain.sh")],
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        plan = json.loads((root / "distill" / "last-plan.json").read_text(encoding="utf-8"))
        assert plan["dry_run"] is True
        assert (root / "health" / "health.jsonl").is_file()
        assert not (root / "distill" / "dream-proposals.json").exists()
        assert "step=decay skipped" in proc.stderr
        assert "FAILED" not in proc.stderr
