#!/usr/bin/env python3
"""把 CLI `stats` 存成带时间戳的 JSON 与一份短笔记。

只调用 `compound-memory stats`（stdout 已是 JSON）。不导入 store，不调用模型
（ADR-0001）。退出码与 `stats` 相同：`COMPOUND_MEMORY_AGENT_ID` 未设置或空白时
为 1，JSON 仍会写下。找不到可执行文件时退出 2，不写文件。

笔记提醒：`uses`、`cross_validated`、`recent_feedback_7d` 是健康度，不是有效性证明。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "docs" / "eval" / "results"


def cli_argv(explicit: str | None) -> list[str]:
    """解析 compound-memory 可执行文件。显式路径优先，其次 PATH，最后 uv run。

    显式路径若是 ``.py``，用当前解释器启动。Windows 的 CreateProcess 不能直接
    执行 ``#!/bin/sh`` 或无扩展名脚本（WinError 193）；POSIX 上也不把 shebang
    当成测试替身的前提。真实的 compound-memory 入口是控制台可执行文件，不走这条。
    """
    if explicit:
        if Path(explicit).suffix.lower() == ".py":
            return [sys.executable, explicit]
        return [explicit]
    found = shutil.which("compound-memory")
    if found:
        return [found]
    uv = shutil.which("uv")
    if uv is None:
        fallback = Path.home() / ".local" / "bin" / "uv"
        if fallback.is_file():
            uv = str(fallback)
    if uv is None:
        raise SystemExit(
            "compound-memory is not on PATH and uv was not found; pass --cli"
        )
    return [uv, "run", "--project", str(REPO), "compound-memory"]


def _sanitize_text(text: str) -> str:
    """自由文本脱敏：仓库绝对前缀与用户 home 前缀换占位符。

    先替换 REPO 再替换 home：REPO 在 home 之下，顺序反了会先把 REPO 前段
    吃掉导致匹配不上。
    """
    text = text.replace(str(REPO), "<repo>")
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    return text


def display_command(command: list[str]) -> list[str]:
    """展示用命令：--root 值只留末段目录名，其余 token 过 _sanitize_text。

    快照笔记会入库，隐私门禁拦本机绝对路径（/Users/<user> 等）；实际执行
    仍用原命令，这里只管写进笔记的展示形态。
    """
    display: list[str] = []
    redact_next = False
    for token in command:
        if redact_next:
            display.append(Path(token).name)
            redact_next = False
            continue
        if token == "--root":
            redact_next = True
            display.append(token)
            continue
        display.append(_sanitize_text(token))
    return display


def build_note(
    *,
    stamp: str,
    command: list[str],
    exit_code: int,
    stderr: str,
    json_name: str,
    stats_ok: bool,
) -> str:
    """给人看的旁注。数字以相邻 JSON 为准，这里不重算。命令与 stderr 先脱敏。"""
    err = _sanitize_text(stderr).strip() or "(empty)"
    payload = "raw stdout (not JSON)" if not stats_ok else f"`{json_name}`"
    lines = [
        f"# stats snapshot {stamp}",
        "",
        "Health snapshot from CLI `stats`. "
        "`uses`, `cross_validated`, and `recent_feedback_7d` support a run log. "
        "They do not show that memory changed task outcomes. "
        "Effectiveness is the A/B sheet in `docs/eval/results/TEMPLATE.md`.",
        "",
        f"- Command: `{' '.join(display_command(command))}`",
        f"- Exit: {exit_code}",
        f"- JSON: {payload}",
        "",
        "stderr:",
        "",
        "```text",
        err,
        "```",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        default=None,
        help="passed through as compound-memory --root (default: CLI default)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DEFAULT_OUT,
        help=f"directory for the snapshot (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--cli",
        default=None,
        help="path to the compound-memory executable",
    )
    args = parser.parse_args(argv)

    command = cli_argv(args.cli)
    if args.root:
        command.extend(["--root", args.root])
    command.append("stats")

    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        print(f"failed to run {' '.join(command)}: {exc}", file=sys.stderr)
        return 2

    stats_ok = True
    try:
        json.loads(proc.stdout)
    except json.JSONDecodeError:
        stats_ok = False

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if stats_ok:
        json_path = args.out_dir / f"{stamp}-stats.json"
        json_path.write_text(proc.stdout, encoding="utf-8")
        json_name = json_path.name
    else:
        raw_path = args.out_dir / f"{stamp}-stats.stdout.txt"
        raw_path.write_text(proc.stdout, encoding="utf-8")
        json_name = raw_path.name
        print(f"stats stdout was not JSON; wrote {raw_path}", file=sys.stderr)

    note_path = args.out_dir / f"{stamp}-stats.md"
    note_path.write_text(
        build_note(
            stamp=stamp,
            command=command,
            exit_code=proc.returncode,
            stderr=proc.stderr,
            json_name=json_name,
            stats_ok=stats_ok,
        ),
        encoding="utf-8",
    )
    print(json_path if stats_ok else note_path)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
