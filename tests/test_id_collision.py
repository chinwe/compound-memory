"""记忆 id 碰撞（#109）：完整 uuid4 hex，写锁内重试，存量短 id 仍可读。

注入固定 uuid4 序列时，第二次落库不得覆盖第一次，两条记忆 id 不同，
词法索引与向量索引各自保留一行（mem_id 不是歧义主键）。
"""

from __future__ import annotations

import json
import re

import pytest

from compound_memory.storage import MemoryStore


class _FixedUUID:
    def __init__(self, hex_value: str) -> None:
        self.hex = hex_value


def _install_uuid_sequence(monkeypatch: pytest.MonkeyPatch, hexes: list[str]) -> None:
    pending = iter(hexes)
    monkeypatch.setattr(
        "compound_memory.storage.facade.uuid.uuid4",
        lambda: _FixedUUID(next(pending)),
    )


def _indexed_ids(store: MemoryStore) -> set[str]:
    data = json.loads((store.root / "index" / "tokens.json").read_text(encoding="utf-8"))
    return {entry["id"] for entry in data["docs"].values()}


def _vector_rows(store: MemoryStore) -> dict[str, tuple[str, str]]:
    assert store.retrieval_status()["mode"] == "vector"
    return store.vector_index._engine.entries()


class TestCollidingUuidSequence:
    def test_same_directory_collision_keeps_both(self, vec_store: MemoryStore, monkeypatch: pytest.MonkeyPatch):
        """同 ns 同 type：旧实现会原子覆盖同一路径。两条正文都要留下，id 不同。"""
        same = "a" * 32
        other = "b" * 32
        _install_uuid_sequence(monkeypatch, [same, same, other])
        first = vec_store.write("alpha collision body", type="episode", source="agent-a")
        second = vec_store.write("beta collision body", type="episode", source="agent-a")
        assert first["id"] == f"20261001_{same}"
        assert second["id"] == f"20261001_{other}"
        assert first["id"] != second["id"]

        alpha = vec_store.ns_root / "_shared" / "episode" / f"{first['id']}.md"
        beta = vec_store.ns_root / "_shared" / "episode" / f"{second['id']}.md"
        assert "alpha collision body" in alpha.read_text(encoding="utf-8")
        assert "beta collision body" in beta.read_text(encoding="utf-8")
        assert "beta collision body" not in alpha.read_text(encoding="utf-8")
        assert {p.name for p in (vec_store.ns_root / "_shared" / "episode").glob("*.md")} == {
            f"{first['id']}.md",
            f"{second['id']}.md",
        }

        assert vec_store.get(first["id"])["content"] == "alpha collision body"
        assert vec_store.get(second["id"])["content"] == "beta collision body"
        assert _indexed_ids(vec_store) >= {first["id"], second["id"]}
        rows = _vector_rows(vec_store)
        assert first["id"] in rows and second["id"] in rows
        assert rows[first["id"]][1] != rows[second["id"]][1]
        assert rows[first["id"]][0] != rows[second["id"]][0]

    def test_cross_directory_collision_does_not_alias(self, vec_store: MemoryStore, monkeypatch: pytest.MonkeyPatch):
        """跨目录同名不会互相覆盖文件，但 find / 向量主键会歧义。重试必须看全局 id。"""
        same = "c" * 32
        other = "d" * 32
        _install_uuid_sequence(monkeypatch, [same, same, other])
        shared = vec_store.write("shared-side collision body", type="fact", source="agent-a")
        private = vec_store.write(
            "private-side collision body",
            type="episode",
            source="agent-b",
            ns="agent-b",
        )
        assert shared["id"] == f"20261001_{same}"
        assert private["id"] == f"20261001_{other}"
        assert (vec_store.ns_root / "_shared" / "fact" / f"{shared['id']}.md").is_file()
        assert (vec_store.ns_root / "agent-b" / "episode" / f"{private['id']}.md").is_file()
        assert not (vec_store.ns_root / "agent-b" / "episode" / f"{shared['id']}.md").exists()

        assert vec_store.get(shared["id"])["content"] == "shared-side collision body"
        assert vec_store.get(private["id"], reader="agent-b")["content"] == "private-side collision body"
        rows = _vector_rows(vec_store)
        assert set(rows) >= {shared["id"], private["id"]}
        assert rows[shared["id"]][1] != rows[private["id"]][1]
        assert _indexed_ids(vec_store) >= {shared["id"], private["id"]}


class TestPersistentCollision:
    def test_exhausted_retries_raise_without_overwrite(self, store: MemoryStore, monkeypatch: pytest.MonkeyPatch):
        """同 id 一直被占：ValueError，已落盘的正文不动。"""
        taken = "e" * 32
        _install_uuid_sequence(monkeypatch, [taken])
        kept = store.write("kept body", type="episode", source="agent-a")
        monkeypatch.setattr(
            "compound_memory.storage.facade.uuid.uuid4",
            lambda: _FixedUUID(taken),
        )
        before = sorted(p.relative_to(store.root).as_posix() for p in store.ns_root.rglob("*.md"))
        with pytest.raises(ValueError, match="memory id collision"):
            store.write("lost body", type="episode", source="agent-a")
        after = sorted(p.relative_to(store.root).as_posix() for p in store.ns_root.rglob("*.md"))
        assert after == before
        text = (store.ns_root / "_shared" / "episode" / f"{kept['id']}.md").read_text(encoding="utf-8")
        assert "kept body" in text
        assert "lost body" not in text


class TestLegacyShortId:
    def test_legacy_short_id_remains_readable(self, store: MemoryStore):
        path = store.ns_root / "_shared" / "fact" / "20261001_abc123.md"
        path.write_text(
            "---\nid: 20261001_abc123\nns: _shared\ntype: fact\n"
            "source: agent-x\ncreated: 2026-10-01\nconfidence: 0.5\n---\n\n"
            "legacy short id body\n",
            encoding="utf-8",
        )
        found = store.find("20261001_abc123")
        assert found is not None
        assert found.content == "legacy short id body"
        assert store.get("20261001_abc123")["content"] == "legacy short id body"

        fresh = store.write("new full id body", type="fact", source="agent-a")
        assert re.fullmatch(r"\d{8}_[0-9a-f]{32}", fresh["id"])
        assert fresh["id"] != "20261001_abc123"
        assert "legacy short id body" in path.read_text(encoding="utf-8")
        assert store.get("20261001_abc123")["content"] == "legacy short id body"
