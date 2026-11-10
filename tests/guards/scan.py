"""护栏测试共用的源码扫描：只看字符串字面量，注释和文档字符串里的说明不算违规。"""

import ast
from collections.abc import Iterator
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "sentinel"


def string_literals(src: str) -> Iterator[tuple[int, str]]:
    tree = ast.parse(src)
    docstrings = {
        id(n.body[0].value)
        for n in ast.walk(tree)
        if isinstance(n, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and n.body
        and isinstance(n.body[0], ast.Expr)
        and isinstance(n.body[0].value, ast.Constant)
    }
    for n in ast.walk(tree):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            yield n.lineno, n.value


def py_files(exclude: tuple[str, ...] = ()) -> Iterator[Path]:
    for p in SRC.rglob("*.py"):
        rel = p.relative_to(SRC).as_posix()
        if not rel.startswith(exclude):
            yield p
