#!/usr/bin/env python3
"""校验 integrations/dsh 这份 DSH bundle，并做一次 npm pack --dry-run。

该目录按未来公开仓库 chinwe/compound-memory-dsh 的根来铺：
根上的 package.json 声明 dsh.bundle，patch 只插入一行宿主自带的
MCP client，stdio 命令固定为
``uvx --from compound-memory compound-memory-server``，
并带上 skills/compound-memory/SKILL.md。没有 install / lifecycle scripts。
不读、不改 Python 包的版本与 MCP tool 面。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "integrations" / "dsh"
ENTRY_TARGET = "data/plugins/chinwe__compound-memory-dsh.yml"
LISTING_URL = "https://github.com/chinwe/compound-memory-dsh"
LISTING_NAME = "chinwe/compound-memory-dsh"
REPO_URL = "git+https://github.com/chinwe/compound-memory-dsh.git"
SKILL_REL = "skills/compound-memory/SKILL.md"
PACK_OK = {"package.json", "cordis.patch.yml", "README.md", SKILL_REL}
LAUNCH_ARGS = ["--from", "compound-memory", "compound-memory-server"]
TOOLS = ("memory_search", "memory_get", "memory_write", "memory_link", "memory_feedback")


class _JsLoader(yaml.SafeLoader):
    """把 Cordis 的 ``!!js`` 标量收成字符串，不执行。"""


def _construct_js(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    if not isinstance(node, yaml.ScalarNode):
        raise yaml.constructor.ConstructorError(
            None, None, "!!js must be a scalar", node.start_mark
        )
    value = loader.construct_scalar(node)
    if not isinstance(value, str):
        raise yaml.constructor.ConstructorError(
            None, None, "!!js scalar was not a string", node.start_mark
        )
    return value


_JsLoader.add_constructor("tag:yaml.org,2002:js", _construct_js)


def fail(message: str) -> None:
    print(f"check_dsh_plugin: {message}", file=sys.stderr)
    raise SystemExit(1)


def load_json(path: Path) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{path.relative_to(ROOT)}: {exc}")
    if not isinstance(data, dict):
        fail(f"{path.relative_to(ROOT)}: expected an object")
    return data


def load_patch(path: Path) -> list[object]:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_JsLoader)
    except yaml.YAMLError as exc:
        fail(f"{path.relative_to(ROOT)}: {exc}")
    if not isinstance(data, list):
        fail("cordis.patch.yml: expected a YAML list")
    return data


def check_package(pkg: dict[str, object]) -> None:
    if pkg.get("name") != "dsh-compound-memory":
        fail(f"package name must be dsh-compound-memory, got {pkg.get('name')!r}")
    if pkg.get("private") is True:
        fail("package is private; dsh plugin add would not publish a layer users can install")
    if "scripts" in pkg:
        fail("package.json must not declare scripts; this bundle has no install or lifecycle hooks")
    engines = pkg.get("engines")
    if not isinstance(engines, dict) or not isinstance(engines.get("dsh"), str) or not engines["dsh"].strip():
        fail("engines.dsh must be a non-empty SemVer range")
    dsh = pkg.get("dsh")
    if not isinstance(dsh, dict):
        fail("package.json: missing dsh object")
    if "client" in dsh:
        fail("dsh.client alone is not an installable bundle; this package must not declare it")
    bundle = dsh.get("bundle")
    if not isinstance(bundle, dict) or bundle.get("patch") != "./cordis.patch.yml":
        fail('dsh.bundle.patch must be "./cordis.patch.yml"')
    files = pkg.get("files")
    if (
        not isinstance(files, list)
        or "cordis.patch.yml" not in files
        or "README.md" not in files
        or "skills" not in files
    ):
        fail('files must include "cordis.patch.yml", "README.md", and "skills"')
    repo = pkg.get("repository")
    if not isinstance(repo, dict):
        fail("repository must be an object pointing at chinwe/compound-memory-dsh")
    if repo.get("url") != REPO_URL:
        fail(f"repository.url must be {REPO_URL}")
    if "directory" in repo:
        fail("repository.directory must be omitted; this package is the future repo root")


def check_patch(doc: list[object]) -> None:
    if len(doc) != 1 or not isinstance(doc[0], dict) or list(doc[0]) != ["insert"]:
        fail("cordis.patch.yml: the only operation must be one insert")
    rows = doc[0]["insert"]
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        fail("cordis.patch.yml: insert exactly one plugin row")
    row = rows[0]
    if row.get("id") != "mcp-compound-memory":
        fail(f"plugin id must be mcp-compound-memory, got {row.get('id')!r}")
    if row.get("name") != "@deepseek-ai/dsh-mcp-client":
        fail("plugin name must be the in-box @deepseek-ai/dsh-mcp-client")
    config = row.get("config")
    if not isinstance(config, dict):
        fail("plugin row missing config")
    if config.get("transport") != "stdio" or config.get("serverName") != "compound-memory":
        fail("config must be stdio serverName compound-memory")
    if config.get("command") != "uvx" or config.get("args") != LAUNCH_ARGS:
        fail("command/args must be: uvx --from compound-memory compound-memory-server")
    env = config.get("env")
    if not isinstance(env, dict):
        fail("config.env must be a map")
    root = env.get("COMPOUND_MEMORY_ROOT")
    agent = env.get("COMPOUND_MEMORY_AGENT_ID")
    if not isinstance(root, str) or "COMPOUND_MEMORY_ROOT" not in root or "$HOME/.agents/memory" not in root:
        fail("COMPOUND_MEMORY_ROOT must default from the environment to $HOME/.agents/memory")
    if not isinstance(agent, str) or "COMPOUND_MEMORY_AGENT_ID" not in agent or "agent-dsh" not in agent:
        fail("COMPOUND_MEMORY_AGENT_ID must default from the environment to agent-dsh")
    if "agent-deepseek" in agent:
        fail("COMPOUND_MEMORY_AGENT_ID default must not be agent-deepseek")
    if config.get("failOnStartupError") is not True:
        fail("failOnStartupError must be true so a missing uvx fails this row loudly")
    raw = (PLUGIN / "cordis.patch.yml").read_text(encoding="utf-8")
    if "agent-deepseek" in raw:
        fail("cordis.patch.yml must not mention agent-deepseek")
    for marker in ("sk-", "ghp_", "AKIA", "BEGIN PRIVATE"):
        if marker in raw:
            fail(f"cordis.patch.yml contains a secret-shaped marker: {marker}")


def check_entry(path: Path) -> None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        fail(f"{path.name}: {exc}")
    if not isinstance(data, dict):
        fail(f"{path.name}: expected a mapping")
    allowed = {"url", "name", "category", "description"}
    extra = set(data) - allowed
    if extra:
        fail(f"{path.name}: unknown keys {sorted(extra)} (awesome-dsh-plugin rejects them)")
    if data.get("url") != LISTING_URL:
        fail(f"{path.name}: url must be {LISTING_URL}")
    if data.get("name") != LISTING_NAME:
        fail(f"{path.name}: name must be {LISTING_NAME}")
    if data.get("category") != "memory":
        fail(f"{path.name}: category must be memory")
    description = data.get("description")
    if not isinstance(description, dict):
        fail(f"{path.name}: description must be a locale map")
    for loc, ending in (("en", "."), ("zh", "。")):
        text = description.get(loc)
        if not isinstance(text, str) or "\n" in text or not text.endswith(ending):
            fail(f"{path.name}: description.{loc} must be one line ending with {ending!r}")
    for tool in TOOLS:
        if tool not in description["en"] or tool not in description["zh"]:
            fail(f"{path.name}: description must name {tool}")


def check_skill(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        fail(f"{SKILL_REL}: must start with YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        fail(f"{SKILL_REL}: frontmatter is not closed")
    try:
        front = yaml.safe_load(text[4:end])
    except yaml.YAMLError as exc:
        fail(f"{SKILL_REL}: {exc}")
    if not isinstance(front, dict):
        fail(f"{SKILL_REL}: frontmatter must be a mapping")
    if front.get("name") != "compound-memory":
        fail(f"{SKILL_REL}: name must be compound-memory")
    description = front.get("description")
    if not isinstance(description, str) or not description.strip():
        fail(f"{SKILL_REL}: description is required")
    if "agent-dsh" not in text:
        fail(f"{SKILL_REL}: must name the host id agent-dsh")
    if "agent-deepseek" in text:
        fail(f"{SKILL_REL}: must not name agent-deepseek")
    for tool in TOOLS:
        if tool not in text:
            fail(f"{SKILL_REL}: must name {tool}")


def check_pack() -> None:
    npm = shutil.which("npm")
    if npm is None:
        fail("npm is not on PATH; npm pack --dry-run was not run")
    proc = subprocess.run(
        [npm, "pack", "--dry-run", "--json"],
        cwd=PLUGIN,
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        fail(f"npm pack --dry-run failed:\n{proc.stderr}")
    try:
        packed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        fail(f"npm pack --json was not JSON: {exc}")
    if not isinstance(packed, list) or not packed or not isinstance(packed[0], dict):
        fail("npm pack --json did not return a package object")
    files = packed[0].get("files")
    if not isinstance(files, list):
        fail("npm pack --json missing files")
    names = set()
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            fail("npm pack files entry missing path")
        names.add(item["path"])
    if names != PACK_OK:
        fail(f"npm pack files must be {sorted(PACK_OK)}, got {sorted(names)}")


def main() -> int:
    package_path = PLUGIN / "package.json"
    patch_path = PLUGIN / "cordis.patch.yml"
    readme_path = PLUGIN / "README.md"
    entry_path = PLUGIN / "awesome-dsh-plugin.entry.yml"
    skill_path = PLUGIN / SKILL_REL
    for path in (package_path, patch_path, readme_path, entry_path, skill_path):
        if not path.is_file():
            fail(f"missing {path.relative_to(ROOT)}")
    check_package(load_json(package_path))
    check_patch(load_patch(patch_path))
    check_entry(entry_path)
    check_skill(skill_path)
    check_pack()
    print(f"check_dsh_plugin: ok (listing target {ENTRY_TARGET})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
