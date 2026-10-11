#!/usr/bin/env bash
# 纯 wheel 冒烟：不装 dev/vec extra，CLI 与 server 入口必须能跑。
# Linux 与 Windows（Git Bash）共用。在仓库根执行。
set -eu

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

rm -rf dist .smoke-venv .smoke-root
uv build
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
