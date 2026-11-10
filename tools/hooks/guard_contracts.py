"""Claude Code PreToolUse hook：阻止直接编辑 src/sentinel/contracts/（08 §6.4；CLAUDE.md"契约写死"）。

负责：Edit / Write / MultiEdit 命中契约目录时，exit 2 阻止，并把原因交给 Claude。
不负责：Bash 里用 sed、重定向等方式改文件——TODO(edge)：要解析 shell 命令，第一版靠 CLAUDE.md 约束。
人确认改契约之后，由人创建 .claude/contracts.unlock 临时放行，改完删掉，并补一条 ADR。
只用标准库：hook 在 uv 环境之外运行。
"""

import json
import os
import sys
from pathlib import Path

PROTECTED = "src/sentinel/contracts/"


def main() -> int:
    event = json.load(sys.stdin)
    path = (event.get("tool_input") or {}).get("file_path") or ""
    root = Path(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".").resolve()
    try:
        rel = Path(path).resolve().relative_to(root).as_posix()
    except ValueError:
        return 0
    if not rel.startswith(PROTECTED):
        return 0
    if (root / ".claude" / "contracts.unlock").exists():
        return 0
    print(
        f"{rel} 是契约文件（03 篇，写死）。修改前先停下来问人；确认后由人创建 .claude/contracts.unlock 放行，"
        "改完删掉，并补一条 ADR。",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
