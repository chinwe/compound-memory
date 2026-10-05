#!/usr/bin/env python3
"""机械检查：storage._git 调用点必须显式传 check=。

_git 默认 check=True——想要容错语义（check=False）时漏传会让降级分支
变成死代码（2026-10-05 code review 实证：_recover_orphan_changes 的
坏仓库降级分支因 CalledProcessError 不可达）。显式 check=True 同样
合法（git_log 的响亮读路径）。pre-commit 与 CI 两层调用（同 privacy
gate 形态，见 docs/agents/privacy-gate.md）；只扫 src/。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "compound_memory"


def main() -> int:
    bad: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_git"
                and not any(kw.arg == "check" for kw in node.keywords)
            ):
                rel = path.relative_to(SRC.parents[1])
                bad.append(f"{rel}:{node.lineno}: _git(...) without explicit check=")
    for line in bad:
        print(line)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
