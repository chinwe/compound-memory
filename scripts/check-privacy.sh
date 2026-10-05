#!/usr/bin/env bash
# 隐私门禁：对指定文件（缺省 = 全部 git 追踪文件）按禁串正则匹配，命中即退出 1。
#
# 禁串本身（姓名/昵称/本机路径）绝不写入仓库——来源优先级：
#   1) $PRIVACY_PATTERNS 环境变量（CI Secret；竖线或换行分隔的正则）
#   2) ~/.config/compound-memory/privacy-patterns.txt（本地文件；一行一个正则，# 开头为注释）
# 两者都缺：警告并放行——本地守卫不硬阻塞未配置的协作者，CI 侧以 Secret 为权威门禁。
# 用法：check-privacy.sh [文件...]；pre-commit hook 传入暂存文件。
# 设计与配置说明：docs/agents/privacy-gate.md
set -u

tmp="$(mktemp)"
list_file="$(mktemp)"
trap 'rm -f "$tmp" "$list_file"' EXIT

if [ -n "${PRIVACY_PATTERNS:-}" ]; then
    printf '%s\n' "$(printf '%s' "$PRIVACY_PATTERNS" | tr '|' '\n')" > "$tmp"
elif [ -f "${HOME}/.config/compound-memory/privacy-patterns.txt" ]; then
    grep -vE '^[[:space:]]*(#|$)' "${HOME}/.config/compound-memory/privacy-patterns.txt" > "$tmp" || true
fi
if [ ! -s "$tmp" ]; then
    echo "privacy-check: no patterns configured (PRIVACY_PATTERNS secret or ~/.config/compound-memory/privacy-patterns.txt); skipping" >&2
    exit 0
fi

if [ "$#" -gt 0 ]; then
    for f in "$@"; do printf '%s\n' "$f" >> "$list_file"; done
else
    git ls-files > "$list_file"
fi

# 逐文件 grep（不用 xargs：grep 的 1=干净 / 2=出错语义必须在循环里保真，xargs 会把两者都折成 123）
hits=""
scan_error=0
while IFS= read -r f; do
    [ -z "$f" ] && continue
    grep -q -f "$tmp" -- "$f" 2>/dev/null
    code=$?
    if [ "$code" -eq 0 ]; then
        hits="${hits}${f}"$'\n'
    elif [ "$code" -gt 1 ]; then
        scan_error=1
    fi
done < "$list_file"

if [ "$scan_error" -ne 0 ]; then
    echo "privacy-check: grep failed on some files; refusing to pass silently" >&2
    exit 2
fi
if [ -n "$hits" ]; then
    echo "privacy-check: banned pattern found in:" >&2
    printf '%s' "$hits" | sed 's/^/  - /' >&2
    echo "privacy-check: rejected. Patterns live OUTSIDE the repo (see docs/agents/privacy-gate.md);" >&2
    echo "  fix the file content, or update the patterns file/secret if a match is legitimate." >&2
    exit 1
fi
exit 0
