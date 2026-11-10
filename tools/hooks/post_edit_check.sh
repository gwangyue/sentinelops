#!/usr/bin/env bash
# Claude Code PostToolUse hook：每次编辑 .py 文件后跑 ruff check 和 ruff format --check（08 §6.4）。
# 有问题时 exit 2，输出会交给 Claude 去修；类型检查太慢，留给 make check。
f=$(python3 -c 'import json,sys; print((json.load(sys.stdin).get("tool_input") or {}).get("file_path") or "")')
case "$f" in *.py) ;; *) exit 0 ;; esac
[ -f "$f" ] || exit 0
cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0
out=$(uv run --quiet ruff check --output-format concise "$f" 2>&1; uv run --quiet ruff format --check "$f" 2>&1) && exit 0
echo "$out" >&2
exit 2
