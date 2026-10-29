"""ADR-0014：从库内 type=skill 导出宿主 SKILL.md。只列候选与点名写出，不进 MCP。"""

from __future__ import annotations

import datetime as dt
import hashlib
import inspect
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from compound_memory import skill_export
from compound_memory.cli import main as cli_main
from compound_memory.model import TYPE_SPEC
from compound_memory.storage import MemoryStore
from compound_memory.storage.distill import SUGGESTED_ACTIONS
from compound_memory.storage.dream import APPLY_SIGNALS, REASON_ACTIVE_SKILL
from conftest import CLOCK_DATE, sandbox_safe_remove

_REPO = Path(__file__).resolve().parents[1]


def _cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str):
    monkeypatch.delenv("COMPOUND_MEMORY_AGENT_ID", raising=False)
    monkeypatch.delenv("COMPOUND_MEMORY_PROJECT", raising=False)
    code = cli_main(list(argv))
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload, captured.err


def _items(directory: Path, rows: list[dict[str, str]]) -> Path:
    path = directory / "items.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _reasons(report: dict) -> dict[str, str]:
    return {row["id"]: row["reason"] for row in report["skipped"]}


def _candidate_ids(report: dict) -> set[str]:
    return {row["id"] for row in report["candidates"]}


def _write_skill(store: MemoryStore, content: str, key: str | None = "fresh-skill", **kwargs) -> dict:
    return store.write(content=content, type="skill", source="agent-a", key=key, **kwargs)


def test_default_candidate_and_one_reason_each(store: MemoryStore):
    """uses=0、confidence=0.5 进候选。每种跳过只给一个原因，其它 type 不出现。"""
    fresh = _write_skill(store, "steps for the fresh skill", key="fresh-skill")
    until_today = _write_skill(
        store, "still valid today", key="until-today", valid_until=CLOCK_DATE.isoformat(),
    )
    archived = _write_skill(store, "will be archived", key="gone-skill")
    archived_mem = store.find(archived["id"])
    assert archived_mem is not None
    archived_mem.created = "not-a-date"
    archived_mem.content = "   "
    archived_mem.key = "BAD"
    archived_mem.confidence = 0.1
    store._archive(archived_mem)

    bad_created = _write_skill(store, "bad clock", key="bad-clock", created="not-a-date")
    bad_until, _conflict = store._write_new(
        "bad until", type="skill", source="agent-a", ns="_shared", key="bad-until",
        links=None, created=CLOCK_DATE.isoformat(), confidence=0.5, origin=None,
        valid_until="not-a-date",
    )
    passed = _write_skill(
        store, "already passed", key="passed-skill",
        valid_until=(CLOCK_DATE - dt.timedelta(days=1)).isoformat(),
    )
    empty = _write_skill(store, "   ", key="empty-skill")
    missing = _write_skill(store, "no key on this skill", key=None)
    bad_key = _write_skill(store, "bad key body", key="temp-key")
    bad_key_mem = store.find(bad_key["id"])
    assert bad_key_mem is not None
    bad_key_mem.key = "Bad_Key"
    store._save(bad_key_mem)
    projected = _write_skill(store, "project only", key="alpha-skill", project="alpha")
    low_conf = _write_skill(store, "low confidence", key="low-conf", confidence=0.4)
    pending = _write_skill(store, "disputed skill", key="pending-skill")
    store.feedback(pending["id"], "agent-a", outcome="contradiction")
    first_dup = _write_skill(store, "dup body one", key="dup-skill")
    second_dup = _write_skill(store, "dup body two", key="dup-skill")
    kept = _write_skill(store, "the unique half", key="half-unique", confidence=0.5)
    dropped = _write_skill(store, "the weak half", key="half-unique", confidence=0.2)
    fact = store.write(content="a fact, not a skill", type="fact", source="agent-a", key="fact-key")
    episode = store.write(content="an episode", type="episode", source="agent-a")
    private = store.write(
        content="private skill", type="skill", source="agent-z", ns="agent-z", key="private-skill",
    )

    report = skill_export.list_candidates(store)
    assert set(report) == {"ns", "candidates", "skipped"}
    assert report["ns"] == "_shared"
    assert isinstance(report["candidates"], list)
    reasons = _reasons(report)
    chosen = _candidate_ids(report)

    assert fresh["id"] in chosen
    fresh_row = next(row for row in report["candidates"] if row["id"] == fresh["id"])
    assert fresh_row["uses"] == 0
    assert fresh_row["confidence"] == 0.5
    assert fresh_row["content"] == "steps for the fresh skill"
    assert fresh_row["project"] is None
    assert fresh_row["key"] == "fresh-skill"
    assert until_today["id"] in chosen
    assert kept["id"] in chosen

    assert reasons[archived["id"]] == "archived"
    assert reasons[bad_created["id"]] == "bad_date"
    assert reasons[bad_until.id] == "bad_date"
    assert reasons[passed["id"]] == "valid_until_passed"
    assert reasons[empty["id"]] == "empty_content"
    assert reasons[missing["id"]] == "missing_key"
    assert reasons[bad_key["id"]] == "bad_key"
    assert reasons[projected["id"]] == "project_mismatch"
    assert reasons[low_conf["id"]] == "below_confidence"
    assert reasons[pending["id"]] == "pending_contradiction"
    assert reasons[first_dup["id"]] == "key_not_unique"
    assert reasons[second_dup["id"]] == "key_not_unique"
    assert reasons[dropped["id"]] == "below_confidence"
    assert fact["id"] not in reasons and fact["id"] not in chosen
    assert episode["id"] not in reasons and episode["id"] not in chosen
    assert private["id"] not in reasons and private["id"] not in chosen
    assert first_dup["id"] not in chosen and second_dup["id"] not in chosen

    # 归档那条同时坏日期、空正文、非法 key、低置信，仍然只有 archived。
    assert [row["reason"] for row in report["skipped"] if row["id"] == archived["id"]] == ["archived"]
    assert not (store.root / "distill").exists()


def test_project_axis_is_exact_and_private_ns_needs_owner(store: MemoryStore, tmp_path: Path):
    global_skill = _write_skill(store, "global steps", key="global-skill")
    alpha = _write_skill(store, "alpha steps", key="alpha-skill", project="alpha")
    beta = _write_skill(store, "beta steps", key="beta-skill", project="beta")

    plain = skill_export.list_candidates(store)
    assert _candidate_ids(plain) == {global_skill["id"]}
    assert _reasons(plain)[alpha["id"]] == "project_mismatch"
    assert _reasons(plain)[beta["id"]] == "project_mismatch"

    scoped = skill_export.list_candidates(store, project="alpha")
    assert _candidate_ids(scoped) == {alpha["id"]}
    assert scoped["candidates"][0]["project"] == "alpha"
    assert _reasons(scoped)[global_skill["id"]] == "project_mismatch"
    assert _reasons(scoped)[beta["id"]] == "project_mismatch"

    private = store.write(
        content="owner steps", type="skill", source="agent-z", ns="agent-z", key="owner-skill",
    )
    with pytest.raises(PermissionError):
        skill_export.list_candidates(store, ns="agent-z")
    with pytest.raises(PermissionError):
        skill_export.list_candidates(store, ns="agent-z", reader="agent-other")
    owned = skill_export.list_candidates(store, ns="agent-z", reader="z")
    assert _candidate_ids(owned) == {private["id"]}
    # _shared 不要求 reader，也不把私有 ns 并进来。
    assert private["id"] not in _candidate_ids(skill_export.list_candidates(store))

    attested = MemoryStore(
        tmp_path / "attested", clock=lambda: CLOCK_DATE, remover=sandbox_safe_remove, agent_id="agent-z",
    )
    attested.write(content="attested steps", type="skill", source="agent-z", ns="agent-z", key="attested-skill")
    with pytest.raises(PermissionError):
        skill_export.list_candidates(attested, ns="agent-z", reader="agent-other")
    assert skill_export.list_candidates(attested, ns="agent-z", reader="z")["candidates"]
    assert skill_export.list_candidates(attested, ns="agent-z")["candidates"]
    with pytest.raises(PermissionError):
        skill_export.list_candidates(attested, ns="_shared", reader="agent-other")


def test_min_uses_is_export_gate_not_promotion(store: MemoryStore):
    fresh = _write_skill(store, "unused skill", key="unused-skill")
    used = _write_skill(store, "used skill", key="used-skill")
    store.feedback(used["id"], "agent-a", outcome="success")
    strict = skill_export.list_candidates(store, min_uses=1)
    assert _reasons(strict)[fresh["id"]] == "below_uses"
    assert used["id"] in _candidate_ids(strict)
    failed = _write_skill(store, "failed once", key="failed-skill")
    store.feedback(failed["id"], "agent-a", outcome="failure")
    default = skill_export.list_candidates(store)
    assert _reasons(default)[failed["id"]] == "below_confidence"
    lowered = skill_export.list_candidates(store, min_confidence=0.2)
    assert failed["id"] in _candidate_ids(lowered)


def test_cli_export_is_object_and_ignores_project_env(
    store: MemoryStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
):
    _write_skill(store, "global body", key="global-skill")
    _write_skill(store, "alpha body", key="alpha-skill", project="alpha")
    monkeypatch.delenv("COMPOUND_MEMORY_AGENT_ID", raising=False)
    monkeypatch.setenv("COMPOUND_MEMORY_PROJECT", "alpha")
    host = tmp_path / "host-skills"
    code = cli_main(["--root", str(store.root), "skill-export"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert isinstance(payload, dict)
    assert [row["key"] for row in payload["candidates"]] == ["global-skill"]
    assert not host.exists()
    assert not (store.root / "distill").exists()
    assert not (store.root / "distill" / "last-plan.json").exists()
    assert not (store.root / "distill" / "dream-proposals.json").exists()


def test_cli_private_ns_and_shared(store: MemoryStore, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    store.write(content="private body", type="skill", source="agent-z", ns="agent-z", key="private-skill")
    monkeypatch.delenv("COMPOUND_MEMORY_PROJECT", raising=False)
    monkeypatch.delenv("COMPOUND_MEMORY_AGENT_ID", raising=False)
    code = cli_main(["--root", str(store.root), "skill-export", "--ns", "agent-z"])
    err = json.loads(capsys.readouterr().err)
    assert code == 2 and "error" in err

    code, payload, _err = _cli(
        monkeypatch, capsys, "--root", str(store.root), "skill-export", "--ns", "agent-z", "--reader", "agent-other",
    )
    assert code == 2 and "error" in json.loads(_err)

    code, payload, _err = _cli(
        monkeypatch, capsys, "--root", str(store.root), "skill-export", "--ns", "agent-z", "--reader", "z",
    )
    assert code == 0
    assert payload["candidates"][0]["key"] == "private-skill"

    monkeypatch.setenv("COMPOUND_MEMORY_AGENT_ID", "agent-z")
    code = cli_main([
        "--root", str(store.root), "skill-export", "--ns", "agent-z", "--reader", "agent-other",
    ])
    assert code == 2
    assert "error" in json.loads(capsys.readouterr().err)


def test_apply_file_shape_and_store_untouched(store: MemoryStore, tmp_path: Path):
    content = "---\nstep one\n<!-- compound-memory-export: id=other ns=_shared key=true -->\nstep two"
    written = _write_skill(store, content, key="true")
    description = 'Say "go": now'
    items = _items(tmp_path, [{"id": written["id"], "description": f"  {description}  "}])
    out = tmp_path / "out"
    before_md = _tree_bytes(store.root)
    head = _git_head(store.root)
    index = (store.root / "index" / "tokens.json").read_bytes()

    report = skill_export.apply_export(store, items, target="dir", out=str(out))
    path = out / "true" / "SKILL.md"
    text = path.read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    loaded = yaml.safe_load(parts[1])
    assert list(loaded) == ["name", "description"]
    assert loaded["name"] == "true"
    assert loaded["description"] == description
    assert parts[1].startswith('name: "')
    assert parts[1].index("name:") < parts[1].index("description:")
    assert "confidence" not in parts[1]
    provenance = f"<!-- compound-memory-export: id={written['id']} ns=_shared key=true -->"
    assert parts[2].startswith(provenance + "\n")
    assert parts[2].split("\n", 1)[1] == content + "\n"
    assert report["results"][0]["action"] == "create"
    assert report["results"][0]["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert report["dry_run"] is False
    assert _tree_bytes(store.root) == before_md
    assert _git_head(store.root) == head
    assert (store.root / "index" / "tokens.json").read_bytes() == index
    assert subprocess.run(
        ["git", "-C", str(store.root), "status", "--porcelain"],
        check=True, capture_output=True, text=True,
    ).stdout == ""


def test_idempotent_update_and_foreign_file(store: MemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    content = "do the thing"
    written = _write_skill(store, content, key="do-thing")
    other = _write_skill(store, "other steps", key="other-thing")
    out = tmp_path / "out"
    items = _items(tmp_path, [
        {"id": written["id"], "description": "when doing the thing"},
        {"key": "other-thing", "description": "when doing the other thing"},
    ])
    calls: list[Path] = []
    real = skill_export.atomic_write_text

    def spy(path: Path, text: str) -> None:
        calls.append(path)
        real(path, text)

    monkeypatch.setattr(skill_export, "atomic_write_text", spy)
    first = skill_export.apply_export(store, items, target="dir", out=str(out))
    assert [row["action"] for row in first["results"]] == ["create", "create"]
    assert [row["key"] for row in first["results"]] == ["do-thing", "other-thing"]
    assert [path.name for path in calls] == ["SKILL.md", "SKILL.md"]

    calls.clear()
    second = skill_export.apply_export(store, items, target="dir", out=str(out))
    assert [row["action"] for row in second["results"]] == ["unchanged", "unchanged"]
    assert calls == []
    assert "sha256" not in second["results"][0]

    mem = store.find(written["id"])
    assert mem is not None
    mem.content = "do the thing, revised"
    store._save(mem)
    revised_items = _items(tmp_path, [{"id": written["id"], "description": "when doing the thing"}])
    # _items overwrites items.json; the other skill is not in this list.
    updated = skill_export.apply_export(store, revised_items, target="dir", out=str(out))
    assert updated["results"][0]["action"] == "updated"
    assert "revised" in (out / "do-thing" / "SKILL.md").read_text(encoding="utf-8")

    desc_items = _items(tmp_path, [{"id": written["id"], "description": "a different trigger"}])
    described = skill_export.apply_export(store, desc_items, target="dir", out=str(out))
    assert described["results"][0]["action"] == "updated"

    foreign = out / "other-thing" / "SKILL.md"
    foreign.write_text("# handwritten\n", encoding="utf-8")
    foreign_bytes = foreign.read_bytes()
    mixed = _items(tmp_path, [
        {"id": written["id"], "description": "a different trigger"},
        {"key": "other-thing", "description": "when doing the other thing"},
    ])
    kept = skill_export.apply_export(store, mixed, target="dir", out=str(out))
    by_key = {row["key"]: row for row in kept["results"]}
    assert by_key["other-thing"]["action"] == "skip"
    assert by_key["other-thing"]["reason"] == "foreign_file"
    assert foreign.read_bytes() == foreign_bytes
    assert by_key["do-thing"]["action"] == "unchanged"

    replaced = skill_export.apply_export(store, mixed, target="dir", out=str(out), replace=True)
    by_key = {row["key"]: row for row in replaced["results"]}
    assert by_key["other-thing"]["action"] == "updated"
    assert "compound-memory-export" in foreign.read_text(encoding="utf-8")
    assert "handwritten" not in foreign.read_text(encoding="utf-8")


def test_dry_run_and_caller_errors(store: MemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    written = _write_skill(store, "dry steps", key="dry-skill")
    items = _items(tmp_path, [{"id": written["id"], "description": "when drying"}])
    workspace = tmp_path / "ws"
    workspace.mkdir()

    def boom(*_args, **_kwargs):
        raise AssertionError("dry-run must not write")

    monkeypatch.setattr(skill_export, "atomic_write_text", boom)
    report = skill_export.apply_export(
        store, items, target="cursor-project", workspace=str(workspace), dry_run=True,
    )
    row = report["results"][0]
    assert row["action"] == "create"
    assert row["path"].endswith(f"{os.sep}.cursor{os.sep}skills{os.sep}dry-skill{os.sep}SKILL.md")
    rendered = skill_export.render_skill_markdown(
        mem_id=written["id"], ns="_shared", key="dry-skill", content="dry steps", description="when drying",
    )
    assert row["sha256"] == hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    assert not (workspace / ".cursor").exists()

    code, _payload, err = _cli(
        monkeypatch, capsys,
        "--root", str(store.root), "skill-export-apply", "--items", str(items),
        "--target", "cursor-project", "--workspace", "relative",
    )
    assert code == 2
    assert "error" in json.loads(err)
    assert not (workspace / ".cursor").exists()

    missing = tmp_path / "no-such-workspace"
    code, _payload, err = _cli(
        monkeypatch, capsys,
        "--root", str(store.root), "skill-export-apply", "--items", str(items),
        "--target", "cursor-project", "--workspace", str(missing),
    )
    assert code == 2 and "error" in json.loads(err)

    code, _payload, err = _cli(
        monkeypatch, capsys,
        "--root", str(store.root), "skill-export-apply", "--items", str(items),
        "--target", "dir", "--out", "relative/out",
    )
    assert code == 2 and "error" in json.loads(err)

    with pytest.raises(SystemExit) as exc:
        cli_main(["--root", str(store.root), "skill-export-apply", "--items", str(items)])
    assert exc.value.code == 2
    assert not (workspace / ".cursor").exists()


def test_cursor_user_root_and_path_refusals(
    store: MemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    written = _write_skill(store, "user steps", key="user-skill")
    canonical = _write_skill(store, "canonical name", key="compound-memory")
    items = _items(tmp_path, [{"id": written["id"], "description": "when using the user skill"}])
    home = tmp_path / "home"
    monkeypatch.setattr(skill_export, "cursor_user_skills_root", lambda: home / ".cursor" / "skills")
    report = skill_export.apply_export(store, items, target="cursor-user")
    path = home / ".cursor" / "skills" / "user-skill" / "SKILL.md"
    assert path.is_file()
    assert report["results"][0]["path"] == str(path)
    assert not (home / ".cursor" / "skills-cursor").exists()
    assert "skills-cursor" not in report["root"]

    with pytest.raises(ValueError, match="memory root"):
        skill_export.apply_export(store, items, target="dir", out=str(store.root))
    assert not any(store.root.rglob("SKILL.md"))

    integrations = tmp_path / "integrations" / "claude" / "skills"
    integrations.mkdir(parents=True)
    with pytest.raises(ValueError, match="integrations"):
        skill_export.apply_export(store, items, target="dir", out=str(integrations))
    assert not (integrations / "user-skill" / "SKILL.md").exists()

    skills_root = tmp_path / "repo" / "skills"
    skills_root.mkdir(parents=True)
    canon_items = _items(tmp_path, [{"id": canonical["id"], "description": "do not write the canonical skill"}])
    with pytest.raises(ValueError, match="canonical"):
        skill_export.apply_export(store, canon_items, target="dir", out=str(skills_root))
    assert not (skills_root / "compound-memory" / "SKILL.md").exists()

    workspace = tmp_path / "ws"
    workspace.mkdir()
    project_items = _items(tmp_path, [{"id": canonical["id"], "description": "user key under .cursor"}])
    placed = skill_export.apply_export(
        store, project_items, target="cursor-project", workspace=str(workspace),
    )
    project_file = workspace / ".cursor" / "skills" / "compound-memory" / "SKILL.md"
    assert project_file.is_file()
    assert placed["results"][0]["action"] == "create"

    outside = tmp_path / "outside"
    outside.mkdir()
    linked = tmp_path / "linked-out"
    linked.mkdir()
    (linked / "user-skill").symlink_to(outside, target_is_directory=True)
    # _items 始终写同一个文件名；这里重写，避免被上面的 compound-memory 条目覆盖。
    escape_items = _items(tmp_path, [{"id": written["id"], "description": "must not follow the symlink"}])
    with pytest.raises(ValueError, match="outside"):
        skill_export.apply_export(store, escape_items, target="dir", out=str(linked))
    assert not (outside / "SKILL.md").exists()


def test_path_blocked_does_not_stop_sibling(store: MemoryStore, tmp_path: Path):
    blocked = _write_skill(store, "blocked steps", key="blocked-key")
    sibling = _write_skill(store, "sibling steps", key="sibling-key")
    out = tmp_path / "out"
    out.mkdir()
    (out / "blocked-key").write_text("not a directory", encoding="utf-8")
    items = _items(tmp_path, [
        {"id": blocked["id"], "description": "blocked"},
        {"id": sibling["id"], "description": "sibling"},
    ])
    report = skill_export.apply_export(store, items, target="dir", out=str(out))
    by_key = {row["key"]: row for row in report["results"]}
    assert by_key["blocked-key"]["reason"] == "path_blocked"
    assert by_key["sibling-key"]["action"] == "create"
    assert (out / "sibling-key" / "SKILL.md").is_file()
    assert (out / "blocked-key").read_text(encoding="utf-8") == "not a directory"


def test_items_shape_rejects_whole_list_before_write(store: MemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    written = _write_skill(store, "body", key="shape-skill")
    out = tmp_path / "out"

    def boom(*_args, **_kwargs):
        raise AssertionError("shape errors must not write")

    monkeypatch.setattr(skill_export, "atomic_write_text", boom)
    cases = [
        [{"id": written["id"]}],
        [{"id": written["id"], "description": "   "}],
        [{"id": written["id"], "description": "one\ntwo"}],
        [{"id": written["id"], "description": "x" * 1025}],
        [{"id": written["id"], "description": "ok", "content": "nope"}],
        [{"id": written["id"], "description": "ok", "note": "extra"}],
        [{"id": written["id"], "key": "shape-skill", "description": "ok"}],
        [{"description": "ok"}],
        [{"id": written["id"], "description": "ok"}, {"key": "shape-skill", "description": "again"}],
    ]
    for rows in cases:
        path = _items(tmp_path, rows)
        with pytest.raises(ValueError):
            skill_export.apply_export(store, path, target="dir", out=str(out))
    assert not out.exists()

    ok = _items(tmp_path, [{"id": written["id"], "description": "y" * 1024}])
    monkeypatch.setattr(skill_export, "atomic_write_text", skill_export.atomic_write_text)
    # 上面的 boom 还在。1024 是合法长度，恢复真写出只为确认边界本身能过形状校验。
    report = skill_export.apply_export(store, ok, target="dir", out=str(out), dry_run=True)
    assert report["results"][0]["action"] == "create"
    assert not out.exists()


def test_not_candidate_is_skipped_and_sibling_is_written(store: MemoryStore, tmp_path: Path):
    live = _write_skill(store, "live steps", key="live-skill")
    stale = _write_skill(store, "stale steps", key="stale-skill")
    mem = store.find(stale["id"])
    assert mem is not None
    store._archive(mem)
    out = tmp_path / "out"
    items = _items(tmp_path, [
        {"id": live["id"], "description": "live"},
        {"id": stale["id"], "description": "stale"},
        {"key": "missing-key", "description": "absent"},
    ])
    report = skill_export.apply_export(store, items, target="dir", out=str(out))
    by_id = {row["id"]: row for row in report["results"] if row["id"]}
    assert by_id[live["id"]]["action"] == "create"
    assert by_id[live["id"]]["key"] == "live-skill"
    assert by_id[stale["id"]]["reason"] == "not_candidate"
    missing = next(row for row in report["results"] if row["key"] == "missing-key")
    assert missing["reason"] == "not_candidate"
    assert (out / "live-skill" / "SKILL.md").is_file()
    assert not (out / "stale-skill").exists()
    assert "failed" not in report


def test_io_error_keeps_completed_file(store: MemoryStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    first = _write_skill(store, "first steps", key="a-skill")
    second = _write_skill(store, "second steps", key="b-skill")
    out = tmp_path / "out"
    items = _items(tmp_path, [
        {"id": first["id"], "description": "a"},
        {"id": second["id"], "description": "b"},
    ])
    real = skill_export.atomic_write_text
    seen = {"n": 0}

    def flaky(path: Path, text: str) -> None:
        seen["n"] += 1
        if seen["n"] == 2:
            raise OSError("disk full")
        real(path, text)

    monkeypatch.setattr(skill_export, "atomic_write_text", flaky)
    report = skill_export.apply_export(store, items, target="dir", out=str(out))
    assert (out / "a-skill" / "SKILL.md").is_file()
    assert not (out / "b-skill" / "SKILL.md").exists()
    assert report["failed"]["key"] == "b-skill"
    assert report["failed"]["error"] == "disk full"
    by_key = {row["key"]: row for row in report["results"]}
    assert by_key["a-skill"]["action"] == "create"
    assert by_key["b-skill"]["reason"] == "io_error"


def test_maintain_dream_and_mcp_stay_put():
    for name in ("daily-maintain.sh", "distill-prepare.sh"):
        text = (_REPO / "scripts" / name).read_text(encoding="utf-8")
        assert "skill-export" not in text
    from compound_memory import cli as cli_mod

    for fn in (cli_mod.cmd_maintain, cli_mod.cmd_dream, cli_mod.cmd_dream_apply, cli_mod.cmd_distill_apply):
        source = inspect.getsource(fn)
        assert "skill_export" not in source
        assert "skill-export" not in source
    skill = TYPE_SPEC["skill"]
    assert skill.ttl_days is None
    assert skill.tau_days == 365.0
    assert skill.weight == 0.9
    assert skill.key_conflicts is True
    assert APPLY_SIGNALS == ("merge_with", "key_duplicates", "promotion_candidate")
    assert REASON_ACTIVE_SKILL == "active skill already exists for this key"
    assert SUGGESTED_ACTIONS == ("merge", "promote", "review")
    assert "is_retrievable" not in inspect.getsource(skill_export._first_skip_reason)
    assert "window_days" not in inspect.getsource(skill_export.list_candidates)
    assert "PROMOTION_USES_THRESHOLD" not in inspect.getsource(skill_export._collect)

    from compound_memory.server import mcp

    names = [tool.name for tool in mcp._tool_manager.list_tools()]
    assert names == ["memory_write", "memory_search", "memory_get", "memory_link", "memory_feedback"]


def test_dream_report_has_no_export_side_effect(store: MemoryStore):
    _write_skill(store, "just a skill", key="quiet-skill")
    report = store.dream()
    assert report["dry_run"] is True
    assert "export" not in report
    assert not (store.root / "distill" / "dream-proposals.json").exists()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for base in (root / "namespaces", root / "archive"):
        if not base.exists():
            continue
        for path in base.rglob("*.md"):
            found[str(path.relative_to(root))] = path.read_bytes()
    return found


def _git_head(root: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
    ).strip()
