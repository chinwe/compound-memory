#!/usr/bin/env bash
# 纯 wheel 冒烟：不装 dev/vec extra，CLI 与 server 入口必须能跑。
# Linux 与 Windows（Git Bash）共用。在仓库根执行。
set -eu

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

rm -rf dist .smoke-venv .smoke-root
uv build

# sdist/wheel 成员名不得包含 Claude / WorkBuddy 市场与插件（不扫文件正文）。
uv run python - <<'PY'
import sys
import tarfile
import zipfile
from pathlib import Path

banned = {".claude-plugin", ".codebuddy-plugin", "integrations", ".mcp.json"}
dist = Path("dist")
archives = sorted(
    p for p in dist.iterdir() if p.name.endswith(".tar.gz") or p.suffix == ".whl"
)
if len(archives) < 2:
    print(f"expected sdist and wheel in {dist}, found {archives}", file=sys.stderr)
    sys.exit(1)


def members(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as zf:
            return zf.namelist()
    with tarfile.open(path) as tf:
        return tf.getnames()


failed = False
for path in archives:
    names = members(path)
    bad = [name for name in names if set(Path(name).parts) & banned]
    if bad:
        failed = True
        print(f"{path.name} contains excluded paths:", file=sys.stderr)
        for name in bad:
            print(f"  {name}", file=sys.stderr)
    else:
        print(
            f"{path.name}: {len(names)} members, "
            "marketplace/plugin paths absent"
        )
if failed:
    sys.exit(1)
PY

uv venv .smoke-venv

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*)
    py=".smoke-venv/Scripts/python.exe"
    bin=".smoke-venv/Scripts"
    ext=".exe"
    ;;
  *)
    py=".smoke-venv/bin/python"
    bin=".smoke-venv/bin"
    ext=""
    ;;
esac

uv pip install --python "$py" dist/*.whl
export COMPOUND_MEMORY_ROOT="${root}/.smoke-root"
"$bin/compound-memory${ext}" init
"$bin/compound-memory${ext}" write "ci smoke entry" episode agent-ci >/dev/null
"$bin/compound-memory${ext}" search "ci smoke" | grep -q "ci smoke entry"
"$bin/compound-memory-server${ext}" </dev/null

rm -rf dist .smoke-venv .smoke-root
echo "wheel smoke ok"
