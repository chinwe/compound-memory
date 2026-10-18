#!/usr/bin/env python3
"""机械检查：市场 catalog 与插件 manifest 的一致性与结构完整性。

市场与插件文件（.claude-plugin / .codebuddy-plugin / integrations/）不进
PyPI 包，也没有任何测试覆盖——改动全靠人肉核验。已真实踩过的坑是版本漂移
（#92 第二个 commit：catalog version 落后 plugin.json，claude plugin
validate --strict 报警）。本脚本把这类结构性断言固化下来，CI 每次跑
（ubuntu × 3.13 格，与 check_git_calls.py 同形态）。

检查项（全部机械可判定）：
- 每份 catalog（.claude-plugin / .codebuddy-plugin 下的 marketplace.json）
  可解析，顶层 name/plugins 齐全，每个条目 name/source/version/description 齐全；
- source 目录相对市场根存在（市场根 = catalog 所在目录的父目录，
  与宿主加载器一致——见 docs/agents/host-plugin-integration.md）；
- 按条目 name 探测插件 manifest（.claude-plugin / .zcode-plugin /
  .codebuddy-plugin 下的 plugin.json），断言 catalog 条目的
  version、name 与 manifest 一致；
- manifest 声明的组件（skills/commands 目录、hooks 文件、mcpServers
  内联或文件）存在且不逃逸插件根；
- hooks.json 的每条 hook command 引用的脚本存在，.sh 脚本有可执行位
  （${CLAUDE_PLUGIN_ROOT} / ${CODEBUDDY_PLUGIN_ROOT} 解析为插件根）；
- 仓库根不得出现 .mcp.json（MANIFEST.in 的 prune 依赖这一点，
  成员名检查在 scripts/wheel_smoke.sh，这里在源头拦）。

只查结构与引用，不查文案内容；宿主自身的 schema 校验
（claude plugin validate 等）仍是权威，本脚本是它的廉价前置。
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# 可选位置参数：仓库根（默认脚本所在仓库）。负例测试指向临时副本，不碰真文件。
ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]

# 市场根下的 catalog 路径（按宿主惯例一个宿主一份；缺文件不报错，
# 宿主未接插件时目录本就不存在——CI 不假设所有宿主市场都在）
CATALOGS = [ROOT / ".claude-plugin" / "marketplace.json", ROOT / ".codebuddy-plugin" / "marketplace.json"]
# 插件 manifest 可能的目录名（探测顺序无关紧要：合法插件只会有一个）
MANIFEST_DIRS = (".claude-plugin", ".zcode-plugin", ".codebuddy-plugin")
# hook command 里的运行时根占位符（宿主各自注入，脚本解析时统一还原为插件根）
PLUGIN_ROOT_VARS = re.compile(r"\$\{(?:CLAUDE|CODEBUDDY|ZCODE)_PLUGIN_ROOT\}")
# 占位符后紧跟的插件内路径。占位符与路径之间允许 shell 引号（"${VAR}"/path 与
# "${VAR}/path"、${VAR}/path 在 shell 里等价，hooks.json 里三种写法都合法）
PLACEHOLDER_PATH = re.compile(r"\$\{(?:CLAUDE|CODEBUDDY|ZCODE)_PLUGIN_ROOT\}[\"']?(/[^\s\"']+)")


class Failures(list[str]):
    def check(self, ok: bool, message: str) -> bool:
        if not ok:
            self.append(message)
        return ok


def load_json(path: Path, fails: Failures, label: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        fails.check(False, f"{label}: unreadable or invalid JSON ({exc})")
        return {}
    if not isinstance(data, dict):
        fails.check(False, f"{label}: top level must be an object")
        return {}
    return data


def check_hooks_json(plugin_root: Path, hooks_path: Path, fails: Failures) -> None:
    label = hooks_path.relative_to(ROOT).as_posix()
    data = load_json(hooks_path, fails, label)
    if not data:
        return
    fails.check(isinstance(data.get("hooks"), dict), f"{label}: missing 'hooks' object")
    events = data.get("hooks")
    if not isinstance(events, dict):
        return
    for event, matchers in sorted(events.items()):
        if not isinstance(matchers, list):
            fails.check(False, f"{label}: {event} must be a list")
            continue
        for matcher in matchers:
            for hook in matcher.get("hooks", []) if isinstance(matcher, dict) else []:
                command = hook.get("command") if isinstance(hook, dict) else None
                if not isinstance(command, str):
                    fails.check(False, f"{label}: {event} hook without a command string")
                    continue
                # 运行时插件根必须走宿主注入的占位符（安装缓存路径由加载器决定，不得猜测）
                if not fails.check(bool(PLUGIN_ROOT_VARS.search(command)), f"{label}: {event} command must reference the ${{..._PLUGIN_ROOT}} placeholder"):
                    continue
                for rel in PLACEHOLDER_PATH.findall(command):
                    script = (plugin_root / rel.lstrip("/")).resolve()
                    exists = script.is_file()
                    fails.check(exists, f"{label}: command references missing file {rel}")
                    if exists and script.suffix == ".sh":
                        fails.check(
                            os.access(script, os.X_OK),
                            f"{label}: {script.relative_to(ROOT).as_posix()} is not executable",
                        )


def check_component(plugin_root: Path, key: str, value, fails: Failures) -> None:
    label = f"{plugin_root.relative_to(ROOT).as_posix()} manifest {key}"
    if key == "mcpServers":
        # 内联配置是 dict；字符串则是指向 .mcp.json 的相对路径
        if isinstance(value, dict):
            return
        if isinstance(value, str):
            target = (plugin_root / value).resolve()
            fails.check(
                target.is_file() and plugin_root.resolve() in target.parents,
                f"{label}: {value} must be a file inside the plugin root",
            )
        else:
            fails.check(False, f"{label}: must be an object or a path string")
        return
    # skills / commands 指目录，hooks 指文件；都在插件根内
    target = (plugin_root / str(value)).resolve()
    inside = plugin_root.resolve() in target.parents or target == plugin_root.resolve()
    fails.check(inside, f"{label}: path escapes the plugin root: {value}")
    expected_type = Path.is_dir if key in ("skills", "commands") else Path.is_file
    kind = "directory" if key in ("skills", "commands") else "file"
    fails.check(expected_type(target), f"{label}: {value} is not a {kind}")


def check_plugin(plugin_root: Path, entry: dict, fails: Failures) -> None:
    manifests = [(plugin_root / d / "plugin.json") for d in MANIFEST_DIRS if (plugin_root / d / "plugin.json").is_file()]
    if not fails.check(len(manifests) == 1, f"{plugin_root.relative_to(ROOT).as_posix()}: expected exactly one plugin manifest, found {manifests}"):
        return
    rel = manifests[0].relative_to(ROOT).as_posix()
    manifest = load_json(manifests[0], fails, rel)
    if not manifest:
        return
    for field in ("name", "version", "description"):
        fails.check(isinstance(manifest.get(field), str) and bool(manifest[field]), f"{rel}: missing '{field}'")
    fails.check(entry.get("name") == manifest.get("name"), f"{rel}: name {manifest.get('name')!r} != catalog entry {entry.get('name')!r}")
    fails.check(entry.get("version") == manifest.get("version"), f"{rel}: version {manifest.get('version')!r} != catalog entry {entry.get('version')!r} (the #92 drift)")
    for key in ("skills", "commands", "hooks", "mcpServers"):
        if key in manifest:
            check_component(plugin_root, key, manifest[key], fails)
    hooks_path = plugin_root / "hooks" / "hooks.json"
    if hooks_path.is_file() or "hooks" in manifest:
        check_hooks_json(plugin_root, hooks_path, fails)


def main() -> int:
    fails = Failures()

    # 源头约束：仓库根不得有 .mcp.json（sdist 成员名检查在 wheel_smoke，这里提前拦）
    fails.check(not (ROOT / ".mcp.json").exists(), "repo root must not contain .mcp.json (see MANIFEST.in)")

    for catalog_path in CATALOGS:
        if not catalog_path.is_file():
            continue
        market_root = catalog_path.parent.parent  # 宿主加载器：<catalog 的父目录>的父目录
        label = catalog_path.relative_to(ROOT).as_posix()
        catalog = load_json(catalog_path, fails, label)
        if not catalog:
            continue
        fails.check(isinstance(catalog.get("name"), str), f"{label}: missing 'name'")
        plugins = catalog.get("plugins")
        if not fails.check(isinstance(plugins, list) and bool(plugins), f"{label}: 'plugins' must be a non-empty list"):
            continue
        names: list[str] = []
        for entry in plugins:
            if not fails.check(isinstance(entry, dict), f"{label}: plugin entry must be an object"):
                continue
            for field in ("name", "source", "version", "description"):
                fails.check(isinstance(entry.get(field), str) and bool(entry[field]), f"{label}: entry missing '{field}'")
            name = entry.get("name", "")
            names.append(name)
            source = entry.get("source", "")
            if not (isinstance(source, str) and source.startswith("./")):
                fails.check(False, f"{label}: entry {name!r} source must be a relative './...' path")
                continue
            plugin_root = (market_root / source).resolve()
            if not fails.check(plugin_root.is_dir(), f"{label}: entry {name!r} source {source} does not exist under the market root"):
                continue
            check_plugin(plugin_root, entry, fails)
        fails.check(len(names) == len(set(names)), f"{label}: duplicate plugin names {names}")

    for line in fails:
        print(line, file=sys.stderr)
    if fails:
        print(f"plugin/marketplace validation failed: {len(fails)} problem(s)", file=sys.stderr)
        return 1
    print("plugin/marketplace validation ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
