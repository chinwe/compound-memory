"""confidence 落库校验（#110）：write_new 单点拒绝 bool / 非有限 / 越界。

CLI distill-apply 与库内写入共用这一处；MCP memory_write 不增加该参数。
"""

from __future__ import annotations

import inspect
import json

import pytest

from compound_memory.cli import main as cli_main
from compound_memory.server import memory_write
from compound_memory.storage import MemoryStore


def _md_paths(store: MemoryStore) -> list[str]:
    return sorted(p.relative_to(store.root).as_posix() for p in store.root.rglob("*.md"))


class TestConfidenceRejectedBeforeDisk:
    @pytest.mark.parametrize(
        "bad",
        [True, False, float("nan"), float("inf"), float("-inf"), -0.01, 1.01, 2, -1],
    )
    def test_write_rejects_without_creating_a_file(self, store: MemoryStore, bad: object):
        before = _md_paths(store)
        with pytest.raises(ValueError, match="confidence must be a finite number"):
            store.write("should not land", type="fact", source="agent-a", confidence=bad)  # type: ignore[arg-type]
        assert _md_paths(store) == before

    def test_write_new_rejects_bool_zero(self, store: MemoryStore):
        """False 数值上等于 0，范围检查会放行；bool 必须在共用落库点被拒。"""
        before = _md_paths(store)
        with pytest.raises(ValueError, match="confidence"):
            store._write_new(
                "internal should not land",
                type="fact",
                source="agent-a",
                ns="_shared",
                key=None,
                links=None,
                created=None,
                confidence=False,  # type: ignore[arg-type]
                origin=None,
            )
        assert _md_paths(store) == before

    def test_distill_apply_rejects_nan_before_disk(self, store: MemoryStore):
        src = store.write("distill source marker", type="episode", source="agent-a")
        before = _md_paths(store)
        with pytest.raises(ValueError, match="confidence"):
            store.distill_apply(
                "product that must not land",
                type="insight",
                source="agent-a",
                source_ids=[src["id"]],
                confidence=float("nan"),
            )
        assert _md_paths(store) == before
        kept = store.find(src["id"])
        assert kept is not None and kept.archived is False
        assert kept.content == "distill source marker"

    def test_cli_distill_apply_rejects_bad_confidence(self, tmp_path, capsys):
        root = tmp_path / "cli-conf"
        assert cli_main(["--root", str(root), "write", "source body", "episode", "agent-a"]) == 0
        mem_id = json.loads(capsys.readouterr().out)["id"]
        for raw in ("nan", "inf", "-inf", "-0.1", "1.5"):
            code = cli_main(
                [
                    "--root",
                    str(root),
                    "distill-apply",
                    "should not land",
                    "insight",
                    "agent-a",
                    "--sources",
                    mem_id,
                    "--confidence",
                    raw,
                ]
            )
            assert code == 2
            err = json.loads(capsys.readouterr().err)
            assert "confidence" in err["error"]
        files = list((root / "namespaces").rglob("*.md"))
        assert len(files) == 1
        assert mem_id in files[0].name
        assert "should not land" not in files[0].read_text(encoding="utf-8")


class TestConfidenceAccepted:
    @pytest.mark.parametrize("value", [0.0, 1.0, 0.5, 0.25])
    def test_boundaries_and_normal_floats_store(self, store: MemoryStore, value: float):
        mem = store.write(f"confidence {value}", type="fact", source="agent-a", confidence=value)
        assert mem["confidence"] == value
        stored = store.find(mem["id"])
        assert stored is not None
        assert stored.confidence == value


class TestMcpDoesNotExposeConfidence:
    def test_memory_write_signature_has_no_confidence(self):
        assert "confidence" not in inspect.signature(memory_write).parameters
