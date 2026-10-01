#!/bin/sh
# distill-prepare：定时蒸馏准备（确定性段，ADR 0001——定时准备 + Agent 按需判断）。
# 跑 distill-plan 把带信号标注的候选清单写到 <root>/distill/last-plan.json；
# 判断段由调用方 Agent 会话内读取该清单完成，取舍经 distill-apply 原子落库。
# 失败要响亮：set -eu 下任何一步失败都以非 0 退出（launchd 日志可见）——
# 静默不跑的蒸馏等于没有蒸馏。
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ROOT="${COMPOUND_MEMORY_ROOT:-$HOME/.agents/memory}"
# launchd 的 PATH 不含 ~/.local/bin：UV_BIN 允许外部覆盖，其次 PATH，最后默认安装位置
UV_BIN="${UV_BIN:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

# last-plan.json 是运行时产物（与 index/ 同类），不进记忆库 git——缺行则补
# （先跑 CLI 让 store 初始化出 .gitignore，再补 distill/ 行）
mkdir -p "$ROOT/distill"
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] distill-prepare: plan -> $ROOT/distill/last-plan.json"
"$UV_BIN" run --project "$REPO_DIR" compound-memory \
  --root "$ROOT" distill-plan > "$ROOT/distill/last-plan.json"
if grep -q '^distill/$' "$ROOT/.gitignore" 2>/dev/null; then :; else
  echo "distill/" >> "$ROOT/.gitignore"
fi
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] distill-prepare: done"
