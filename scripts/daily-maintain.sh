#!/bin/sh
# daily-maintain：launchd/cron 薄包装。编排在 CLI `compound-memory maintain`
# （只读 dream → 可选 decay → health-snapshot）。本脚本不写报告、不决定步骤。
# decay 默认关。要衰减时给命令加 --with-decay，或设 RUN_DECAY=1 / DECAY=1
# （CLI 读取这两个环境变量，脚本原样继承，不在这里翻译）。
# 不传 --propose，不调用 dream-apply / distill-apply（ADR-0001 / #72 / #97）。
# distill-prepare.sh 仍是只跑 dream 的准备路径，默认行为不变。
# 失败要响亮：set -eu 下 maintain 非 0 则本脚本非 0（launchd/cron 可见）。
set -eu

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
# launchd 的 PATH 不含 ~/.local/bin：UV_BIN 允许外部覆盖，其次 PATH，最后默认安装位置
UV_BIN="${UV_BIN:-$(command -v uv || echo "$HOME/.local/bin/uv")}"

echo "[$(date '+%Y-%m-%dT%H:%M:%S')] daily-maintain: exec compound-memory maintain"
# 根目录由 CLI 的 default_root() 解析（COMPOUND_MEMORY_ROOT / ~/.agents/memory）。
# shell 不展开 ~，也不自己拼库路径。额外参数原样传给 maintain（例如 --with-decay）。
"$UV_BIN" run --project "$REPO_DIR" compound-memory maintain "$@"
echo "[$(date '+%Y-%m-%dT%H:%M:%S')] daily-maintain: done"
