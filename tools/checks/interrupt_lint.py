"""AST 检查：`interrupt()` 不能被宽泛的 except 包住（08 §6.4）。

原因：LangGraph 的 interrupt() 靠抛出 GraphInterrupt 来暂停图。如果它在 try 里，而 except 捕获了
Exception / BaseException 或者是裸 except，暂停信号就会被吞掉，审批等于没发生。
负责：扫描给定目录下的 .py 文件，报告违规位置，有违规就非零退出。
不负责：跨函数的情况（try 里调用的函数内部再调 interrupt）——TODO(edge)：需要调用图分析。
"""

import ast
import sys
from pathlib import Path

BROAD = {"Exception", "BaseException"}


def _is_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = t.elts if isinstance(t, ast.Tuple) else [t]
    return any(isinstance(n, ast.Name) and n.id in BROAD for n in names)


def _calls_interrupt(nodes: list[ast.stmt]) -> int | None:
    for stmt in nodes:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call):
                f = n.func
                name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                if name == "interrupt":
                    return n.lineno
    return None


def check_source(src: str, filename: str = "<src>") -> list[str]:
    errors = []
    for node in ast.walk(ast.parse(src, filename)):
        if isinstance(node, ast.Try | ast.TryStar) and any(_is_broad(h) for h in node.handlers):
            line = _calls_interrupt(node.body)
            if line is not None:
                errors.append(f"{filename}:{line}: interrupt() 被宽泛的 except 包住，暂停信号会被吞掉")
    return errors


def main(argv: list[str]) -> int:
    errors: list[str] = []
    for root in argv or ["src"]:
        for p in sorted(Path(root).rglob("*.py")):
            errors += check_source(p.read_text(encoding="utf-8"), str(p))
    for e in errors:
        print(e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
