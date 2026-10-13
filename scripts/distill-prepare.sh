#!/bin/sh
# distill-prepare：定时蒸馏准备（确定性段，ADR 0001——定时准备 + Agent 按需判断）。
# 跑 dream（只读，无 LLM）把报告写到 <root>/distill/last-plan.json：
# distill 段即 distill-plan 候选清单，另有 decay_signals 与 stats 快照。
# 判断段由调用方 Agent 会话内读取该报告完成，取舍经 distill-apply 原子落库。
# dream 自身不归档、不 distill-apply、不 forget。
# 定时路径永远只跑只读 dream（#72）。不要改成带 apply 的命令：落库必须由人/Agent
# 显式触发，并且先给出产物正文。
# 失败要响亮：set -eu 下任何一步失败都以非 0 退出（launchd 日志可见）——
# 静默不跑的蒸馏等于没有蒸馏。
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# launchd 的 PATH 不含 ~/.local/bin：UV_BIN 允许外部覆盖，其次 PATH，最后默认安装位置
UV_BIN="${UV_BIN:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
# 根目录只经 default_root()（~ / $HOME 展开、空白回退）。shell 不会展开变量值里的 ~，
# 这里若自己拼 COMPOUND_MEMORY_ROOT 会建成字面量 ~ 目录，和默认库分裂。
ROOT="$("$UV_BIN" run --project "$REPO_DIR" python -c 'from compound_memory.storage import default_root; print(default_root())')"

# last-plan.json 是运行时产物（dream 报告）；<root>/.gitignore 由 store 的 _ensure_layout 统一管理
# （index/ 与 distill/ 都在其中）——脚本只负责建目录与写出报告
mkdir -p "$ROOT/distill"
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] distill-prepare: dream -> $ROOT/distill/last-plan.json"
"$UV_BIN" run --project "$REPO_DIR" compound-memory \
  --root "$ROOT" dream --ns _shared > "$ROOT/distill/last-plan.json"
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] distill-prepare: done"
