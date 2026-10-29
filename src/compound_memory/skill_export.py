"""把库内 type=skill 导出成宿主 SKILL.md（ADR-0014）。

CLI-only 运维动词（skill-export / skill-export-apply），不进 MCP，不进
MemoryStore 的写动词表。server 零 LLM：不摘要、不翻译、不从正文生成
description。description 只来自调用方的 items 文件。

description 上限 1024 字符。Cursor 的 skill 文档
（https://cursor.com/docs/skills）要求 description，但没有给出更小的字符上限。
与之对齐的 Agent Skills 规范（https://agentskills.io/specification）把
description 定在最多 1024 字符。ADR 说「宿主文档若更严就取更严」，这里没有
更严的数字，所以保持 1024。

导出门槛是本模块自己的缺省（uses >= 0、confidence >= 0.5），不读
PROMOTION_USES_THRESHOLD，也不读 distill / dream 的 window。project 轴是
精确相等，不走检索的「全局 ∪ 项目」并集，也不调用 is_retrievable。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterator, Protocol

import yaml

from .index import atomic_write_text
from .model import Memory
from .review_queue import ReviewQueue
from .scoring import is_expired, recency_age
from .storage.validation import _KEY_RE, check_ns, check_ns_owner, check_project, resolve_identity

# 导出命令自己的缺省。与 distill-plan 的 min_uses=1 无关：新写入的 skill
# 起点就是 uses=0，缺省 1 会把刚落库、还没有 feedback 的 skill 挡在外面。
DEFAULT_MIN_USES = 0
DEFAULT_MIN_CONFIDENCE = 0.5

# 见模块 docstring：Cursor 文档没有更小上限，Agent Skills 规范与 ADR 同为 1024。
MAX_DESCRIPTION_CHARS = 1024

_TARGETS = ("cursor-project", "cursor-user", "dir")

# 第一遍跳过原因，顺序即优先级（一条记忆只给一个）。key_not_unique 是第二遍。
_SKIP_PRIORITY = (
    "archived",
    "bad_date",
    "valid_until_passed",
    "empty_content",
    "missing_key",
    "bad_key",
    "project_mismatch",
    "below_uses",
    "below_confidence",
    "pending_contradiction",
)

_PROVENANCE_RE = re.compile(
    r"^<!-- compound-memory-export: id=(\S+) ns=(\S+) key=(\S+) -->$"
)

_ITEM_KEYS = frozenset({"id", "key", "description"})


class _Quoted(str):
    """YAML 双引号标量。只包值，不包键，避免 default_style 把键也引上。"""


class _SkillDumper(yaml.SafeDumper):
    """导出壳专用。不改全局 SafeDumper，记忆文件的 yaml.safe_dump 不受影响。"""


def _represent_quoted(dumper: yaml.SafeDumper, data: _Quoted) -> yaml.Node:
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')


_SkillDumper.add_representer(_Quoted, _represent_quoted)


class _ExportStore(Protocol):
    """skill-export 读库用到的 store 面。不新增 MemoryStore 方法。"""

    root: Path
    ns_root: Path
    archive_root: Path
    agent_id: str | None
    _review_queue: ReviewQueue

    def today(self) -> str: ...

    def _scan_parsed(self, base: Path) -> Iterator[tuple[Memory, Path]]: ...


def cursor_user_skills_root() -> Path:
    """Cursor 用户 skill 目录。不是 ~/.cursor/skills-cursor（那是 Cursor 自带 skill）。"""
    return Path.home() / ".cursor" / "skills"


def _today(store: _ExportStore) -> dt.date:
    """注入 clock 的公开出口。测试钉 CLOCK_DATE 时这里不是墙钟。"""
    return dt.date.fromisoformat(store.today())


def _gate(store: _ExportStore, ns: str, reader: str | None, project: str | None) -> None:
    """身份门在扫描之前。私有 ns 缺 reader 或不是属主就拒绝；_shared 不要求 reader。"""
    check_ns(ns)
    identity = resolve_identity(reader, "reader", store.agent_id)
    check_ns_owner(ns, identity)
    if project is not None:
        check_project(project)


def _project_label(mem: Memory) -> str | None:
    project = mem.project
    if isinstance(project, str) and project:
        return project
    return None


def _valid_until_unparseable(value: object) -> bool:
    """valid_until 缺省放行。有值但不是可解析的 ISO 日期则算坏日期，不冒充已过。"""
    if value is None or value == "":
        return False
    if isinstance(value, str):
        try:
            dt.date.fromisoformat(value)
        except ValueError:
            return True
        return False
    return True


def _first_skip_reason(
    mem: Memory,
    today: dt.date,
    project: str | None,
    min_uses: int,
    min_confidence: float,
    pending: set[str],
) -> str | None:
    """第一遍谓词。返回 None 表示通过，进入 key 唯一性那一遍。"""
    if mem.archived:
        return "archived"
    if recency_age(mem, today) is None or _valid_until_unparseable(mem.valid_until):
        return "bad_date"
    if is_expired(mem, today):
        return "valid_until_passed"
    if not isinstance(mem.content, str) or mem.content.strip() == "":
        return "empty_content"
    if mem.key is None or mem.key == "":
        return "missing_key"
    if not isinstance(mem.key, str) or _KEY_RE.match(mem.key) is None:
        return "bad_key"
    if _project_label(mem) != project:
        return "project_mismatch"
    if mem.uses < min_uses:
        return "below_uses"
    if mem.confidence < min_confidence:
        return "below_confidence"
    if mem.id in pending:
        return "pending_contradiction"
    return None


def _skip_row(mem: Memory, reason: str) -> dict[str, Any]:
    return {"id": mem.id, "ns": mem.ns, "key": mem.key, "reason": reason}


def _candidate_row(mem: Memory) -> dict[str, Any]:
    return {
        "id": mem.id,
        "ns": mem.ns,
        "key": mem.key,
        "project": _project_label(mem),
        "uses": mem.uses,
        "confidence": mem.confidence,
        "content": mem.content,
    }


def _skills_in_ns(store: _ExportStore, ns: str) -> list[Memory]:
    """该 ns 的 type=skill，活动区与归档区都扫。其它 type 不出现。"""
    seen: set[str] = set()
    found: list[Memory] = []
    for base in (store.ns_root / ns, store.archive_root / ns):
        if not base.exists():
            continue
        for mem, _path in store._scan_parsed(base):
            if mem.type != "skill" or mem.ns != ns or mem.id in seen:
                continue
            seen.add(mem.id)
            found.append(mem)
    return found


def _collect(
    store: _ExportStore,
    ns: str,
    reader: str | None,
    project: str | None,
    min_uses: int,
    min_confidence: float,
) -> tuple[list[Memory], list[dict[str, Any]]]:
    """候选与跳过。门禁失败时还不扫目录。"""
    _gate(store, ns, reader, project)
    today = _today(store)
    pending = store._review_queue.pending_contradictions()
    skipped: list[dict[str, Any]] = []
    passed: list[Memory] = []
    for mem in _skills_in_ns(store, ns):
        reason = _first_skip_reason(mem, today, project, min_uses, min_confidence, pending)
        if reason is None:
            passed.append(mem)
        else:
            skipped.append(_skip_row(mem, reason))
    by_key: dict[str, list[Memory]] = {}
    for mem in passed:
        # 第一遍已保证 key 是匹配 _KEY_RE 的字符串。
        assert isinstance(mem.key, str)
        by_key.setdefault(mem.key, []).append(mem)
    eligible: list[Memory] = []
    for group in by_key.values():
        if len(group) != 1:
            for mem in group:
                skipped.append(_skip_row(mem, "key_not_unique"))
            continue
        eligible.append(group[0])
    eligible.sort(key=lambda mem: (mem.key or "", mem.id))
    skipped.sort(key=lambda row: (row["id"], row["reason"]))
    return eligible, skipped


def list_candidates(
    store: _ExportStore,
    ns: str = "_shared",
    reader: str | None = None,
    project: str | None = None,
    min_uses: int = DEFAULT_MIN_USES,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """只读清单。不创建宿主目录，不写 distill/，不改记忆文件。"""
    eligible, skipped = _collect(store, ns, reader, project, min_uses, min_confidence)
    return {
        "ns": ns,
        "candidates": [_candidate_row(mem) for mem in eligible],
        "skipped": skipped,
    }


def render_skill_markdown(
    *,
    mem_id: str,
    ns: str,
    key: str,
    content: str,
    description: str,
) -> str:
    """固定壳 + 溯源行 + content 原文。content 末尾不是换行时补一个，使文件是文本文件。"""
    dumped = yaml.dump(
        {"name": _Quoted(key), "description": _Quoted(description)},
        Dumper=_SkillDumper,
        allow_unicode=True,
        sort_keys=False,
        width=2**31,
    )
    if not dumped.endswith("\n"):
        dumped += "\n"
    provenance = f"<!-- compound-memory-export: id={mem_id} ns={ns} key={key} -->\n"
    body = content if content.endswith("\n") else content + "\n"
    return f"---\n{dumped}---\n{provenance}{body}"


def _load_items(path: Path) -> list[Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"cannot read skill-export items: {path}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"skill-export items must be JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise ValueError("skill-export items must be a JSON list")
    return data


def _validate_item(item: Any, index: int) -> tuple[str | None, str | None, str]:
    """形状错误整份拒绝。content 出现表示调用方以为导出可以改正文。"""
    if not isinstance(item, dict):
        raise ValueError(f"skill-export items[{index}] must be an object")
    if "content" in item:
        raise ValueError(
            f"skill-export items[{index}] must not include content; "
            "the host file body is the stored memory content"
        )
    extra = set(item) - _ITEM_KEYS
    if extra:
        raise ValueError(
            f"skill-export items[{index}] has unexpected keys: {sorted(extra)}"
        )
    has_id = "id" in item
    has_key = "key" in item
    if has_id == has_key:
        raise ValueError(f"skill-export items[{index}] needs exactly one of id or key")
    if "description" not in item:
        raise ValueError(f"skill-export items[{index}] needs description")
    description = item["description"]
    if not isinstance(description, str):
        raise ValueError(f"skill-export items[{index}] description must be a string")
    description = description.strip()
    if description == "":
        raise ValueError(f"skill-export items[{index}] description is empty")
    if "\n" in description or "\r" in description:
        raise ValueError(f"skill-export items[{index}] description must be a single line")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"skill-export items[{index}] description exceeds {MAX_DESCRIPTION_CHARS} characters "
            f"({len(description)})"
        )
    ident: str | None = None
    key: str | None = None
    if has_id:
        raw_id = item["id"]
        if not isinstance(raw_id, str) or raw_id == "":
            raise ValueError(f"skill-export items[{index}] id must be a non-empty string")
        ident = raw_id
    else:
        raw_key = item["key"]
        if not isinstance(raw_key, str) or raw_key == "":
            raise ValueError(f"skill-export items[{index}] key must be a non-empty string")
        key = raw_key
    return ident, key, description


def resolve_target_root(
    target: str,
    workspace: str | None,
    out: str | None,
) -> Path:
    """三个目标没有缺省。相对路径拒绝：本仓库里 cwd 会在调用之间漂移。"""
    if target not in _TARGETS:
        raise ValueError(f"--target must be one of {', '.join(_TARGETS)}, got: {target!r}")
    if target == "cursor-project":
        if not workspace:
            raise ValueError("cursor-project requires an absolute --workspace")
        if out:
            raise ValueError("cursor-project does not take --out")
        path = Path(workspace)
        if not path.is_absolute():
            raise ValueError("--workspace must be an absolute path")
        if not path.is_dir():
            raise ValueError(f"--workspace does not exist: {path}")
        return path / ".cursor" / "skills"
    if target == "cursor-user":
        if workspace or out:
            raise ValueError("cursor-user does not take --workspace or --out")
        return cursor_user_skills_root()
    if not out:
        raise ValueError("dir requires an absolute --out")
    if workspace:
        raise ValueError("dir does not take --workspace")
    path = Path(out)
    if not path.is_absolute():
        raise ValueError("--out must be an absolute path")
    if path.exists() and not path.is_dir():
        raise ValueError(f"--out exists and is not a directory: {path}")
    return path


def _refusal(dest: Path, target_root: Path, memory_root: Path) -> str | None:
    """目标越界：记忆库根、integrations 组件、规范源。符号链接指出根外同样拒绝。"""
    resolved = dest.resolve()
    try:
        resolved.relative_to(target_root.resolve())
    except ValueError:
        return f"target {resolved} is outside {target_root.resolve()}"
    try:
        resolved.relative_to(memory_root.resolve())
    except ValueError:
        pass
    else:
        return f"target {resolved} is inside the memory root {memory_root.resolve()}"
    if "integrations" in resolved.parts:
        return f"target {resolved} has an integrations path component"
    parts = resolved.parts
    if len(parts) >= 3 and parts[-3:] == ("skills", "compound-memory", "SKILL.md"):
        previous = parts[-4] if len(parts) >= 4 else None
        if previous != ".cursor":
            return f"target {resolved} is the canonical skills/compound-memory/SKILL.md"
    return None


def _provenance_id(data: bytes) -> str | None:
    """只认紧挨 frontmatter 结束的那一行。正文里相同字样不算。"""
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        return None
    if not text.startswith("---\n"):
        return None
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return None
    line = parts[2].split("\n", 1)[0]
    matched = _PROVENANCE_RE.match(line)
    if matched is None:
        return None
    return matched.group(1)


def _classify(dest: Path, rendered: bytes, mem_id: str, replace: bool) -> tuple[str, str | None]:
    """create / update / unchanged，或 skip + reason。不在这里写文件。"""
    parent = dest.parent
    if parent.exists() and not parent.is_dir():
        return "skip", "path_blocked"
    if dest.exists() and not dest.is_file():
        return "skip", "path_blocked"
    if not dest.exists():
        return "create", None
    existing = dest.read_bytes()
    provenance = _provenance_id(existing)
    same = existing == rendered
    if provenance != mem_id and not replace:
        return "skip", "foreign_file"
    if same:
        return "unchanged", None
    return "update", None


def _row_sort_key(item: tuple[Memory | None, str | None, str | None, str]) -> tuple[str, str]:
    """报告与写出都按 key 稳定排序。还没对上记忆时用 items 里的 key / id。"""
    mem, ident, key, _description = item
    if mem is not None and mem.key:
        primary = mem.key
    else:
        primary = key or ""
    secondary = mem.id if mem is not None else (ident or "")
    return primary, secondary


def _public_action(action: str, dry_run: bool) -> str:
    """dry-run 用 update；真正写出用 updated（ADR 两处用词）。"""
    if action == "update":
        return "update" if dry_run else "updated"
    return action


def apply_export(
    store: _ExportStore,
    items_path: Path,
    *,
    target: str,
    workspace: str | None = None,
    out: str | None = None,
    ns: str = "_shared",
    reader: str | None = None,
    project: str | None = None,
    min_uses: int = DEFAULT_MIN_USES,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    replace: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """按 items 点名写出。形状错误、同路径、目标越界在任何写出之前拒绝整份列表。

    每条在写之前重跑候选谓词（本次调用现扫，不信任更早的 skill-export 清单）。
    不再合格的进 skipped，原因 not_candidate。合格与跳过混在一次里时，合格的照写。
    """
    parsed = [_validate_item(item, index) for index, item in enumerate(_load_items(items_path))]
    target_root = resolve_target_root(target, workspace, out)
    eligible, _skipped = _collect(store, ns, reader, project, min_uses, min_confidence)
    by_id = {mem.id: mem for mem in eligible}
    by_key = {mem.key: mem for mem in eligible}

    resolved: list[tuple[Memory | None, str | None, str | None, str]] = []
    for ident, key, description in parsed:
        mem: Memory | None
        if ident is not None:
            mem = by_id.get(ident)
        else:
            mem = by_key.get(key) if key is not None else None
        resolved.append((mem, ident, key, description))

    destinations: list[tuple[int, Memory, Path]] = []
    seen_paths: dict[str, int] = {}
    for index, (mem, _ident, _key, _description) in enumerate(resolved):
        if mem is None or not isinstance(mem.key, str):
            continue
        dest = target_root / mem.key / "SKILL.md"
        token = str(dest)
        if token in seen_paths:
            raise ValueError(
                f"skill-export items resolve to the same target path: {dest}"
            )
        seen_paths[token] = index
        refusal = _refusal(dest, target_root, store.root)
        if refusal is not None:
            raise ValueError(refusal)
        destinations.append((index, mem, dest))

    # 写之前的拒绝已经结束。下面才分类、才可能创建目录。
    plan: dict[int, tuple[str, str | None, bytes, Path]] = {}
    for index, mem, dest in destinations:
        _ident, _key, description = parsed[index][0], parsed[index][1], parsed[index][2]
        assert isinstance(mem.key, str)
        rendered = render_skill_markdown(
            mem_id=mem.id,
            ns=mem.ns,
            key=mem.key,
            content=mem.content,
            description=description,
        ).encode("utf-8")
        action, reason = _classify(dest, rendered, mem.id, replace)
        plan[index] = (action, reason, rendered, dest)

    results: list[dict[str, Any]] = []
    failed: dict[str, Any] | None = None
    write_order = sorted(
        destinations,
        key=lambda item: (item[1].key or "", item[1].id),
    )
    stop = False
    for index, mem, dest in write_order:
        action, reason, rendered, dest = plan[index]
        if stop and action in {"create", "update"}:
            # 前面的写出已经失败。剩下的合格项不再写，也不报成 create。
            plan[index] = ("skip", "not_attempted", rendered, dest)
            continue
        if action in {"create", "update"} and not dry_run:
            try:
                atomic_write_text(dest, rendered.decode("utf-8"))
            except OSError as exc:
                failed = {
                    "id": mem.id,
                    "key": mem.key,
                    "path": str(dest),
                    "error": str(exc),
                }
                plan[index] = ("skip", "io_error", rendered, dest)
                stop = True

    # 报告顺序与写出顺序相同：按 key。未进入 plan 的是 not_candidate。
    order = sorted(range(len(parsed)), key=lambda index: _row_sort_key(resolved[index]))
    for index in order:
        mem, ident, key, _description = resolved[index]
        if index not in plan:
            row = {
                "id": ident if ident is not None else (mem.id if mem is not None else None),
                "key": key if key is not None else (mem.key if mem is not None else None),
                "action": "skip",
                "reason": "not_candidate",
            }
            results.append(row)
            continue
        action, reason, rendered, dest = plan[index]
        assert mem is not None
        shown = _public_action(action, dry_run)
        row = {
            "id": mem.id,
            "key": mem.key,
            "path": str(dest),
            "action": shown,
        }
        if action in {"create", "update"}:
            row["sha256"] = hashlib.sha256(rendered).hexdigest()
        if reason is not None:
            row["reason"] = reason
            if reason == "io_error" and failed is not None:
                row["error"] = failed["error"]
        results.append(row)

    skipped = [row for row in results if row["action"] == "skip"]
    report: dict[str, Any] = {
        "ns": ns,
        "target": target,
        "root": str(target_root),
        "dry_run": dry_run,
        "results": results,
        "skipped": skipped,
    }
    if failed is not None:
        report["failed"] = failed
    return report


# 跳过原因优先级导出给测试核对，避免测试再抄一份容易漂移的元组。
SKIP_REASONS = _SKIP_PRIORITY + ("key_not_unique",)
