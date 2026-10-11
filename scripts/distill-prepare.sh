#!/bin/sh
# distill-prepare：定时蒸馏准备（确定性段，ADR 0001——定时准备 + Agent 按需判断）。
# 跑 distill-plan 把带信号标注的候选清单写到 <root>/distill/last-plan.json；
# 判断段由调用方 Agent 会话内读取该清单完成，取舍经 distill-apply 原子落库。
# 失败要响亮：set -eu 下任何一步失败都以非 0 退出（launchd 日志可见）——
# 静默不跑的蒸馏等于没有蒸馏。
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# launchd 的 PATH 不含 ~/.local/bin：UV_BIN 允许外部覆盖，其次 PATH，最后默认安装位置
UV_BIN="${UV_BIN:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
# 根目录只经 default_root()（~ / $HOME 展开、空白回退）。shell 不会展开变量值里的 ~，
# 这里若自己拼 COMPOUND_MEMORY_ROOT 会建成字面量 ~ 目录，和默认库分裂。
ROOT="$("$UV_BIN" run --project "$REPO_DIR" python -c 'from compound_memory.storage import default_root; print(default_root())')"

# last-plan.json 是运行时产物；<root>/.gitignore 由 store 的 _ensure_layout 统一管理
# （index/ 与 distill/ 都在其中）——脚本只负责建目录与产出 plan
mkdir -p "$ROOT/distill"
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] distill-prepare: plan -> $ROOT/distill/last-plan.json"
"$UV_BIN" run --project "$REPO_DIR" compound-memory \
  --root "$ROOT" distill-plan > "$ROOT/distill/last-plan.json"
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] distill-prepare: done"
