"""写凭证只出现在 exec/（08 §6.4；ADR-005）。

写凭证名来自所有 pack.yaml 的 connection.write_credential，加上已知名称；扫描 exec/ 以外的字符串字面量。
"""

from pathlib import Path

import yaml

from tests.guards.scan import SRC, py_files, string_literals

KNOWN = {"es-executor"}


def write_credential_names() -> set[str]:
    names = set(KNOWN)
    for p in (SRC / "packs").rglob("pack.yaml"):
        wc = (yaml.safe_load(p.read_text(encoding="utf-8")).get("connection") or {}).get("write_credential")
        if wc:
            names.add(wc)
    return names


def find_refs(src: str, names: set[str]) -> list[tuple[int, str]]:
    return [(ln, s) for ln, s in string_literals(src) if any(n in s for n in names)]


def test_detector_catches_violation() -> None:
    assert find_refs('cred = load("es-executor")\n', {"es-executor"})


def test_write_credential_only_in_exec() -> None:
    names = write_credential_names()
    hits = [
        f"{p}:{ln}: {s!r}"
        for p in py_files(exclude=("exec/",))
        for ln, s in find_refs(Path(p).read_text(encoding="utf-8"), names)
    ]
    assert not hits, "写凭证只能在 exec/ 里引用：\n" + "\n".join(hits)
