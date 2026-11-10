"""型号只在 config/models.yaml 里出现，src/ 不写型号字符串（ADR-008，08 §6.4）。"""

import re

from tests.guards.scan import py_files, string_literals

MODEL_ID = re.compile(r"\b(gemini|claude|gpt)-[\w.\-]+", re.I)


def find_model_ids(src: str) -> list[tuple[int, str]]:
    return [(ln, s) for ln, s in string_literals(src) if MODEL_ID.search(s)]


def test_detector_catches_violation() -> None:
    assert find_model_ids('MODEL = "gemini-3.7-flash"\n')
    assert find_model_ids('"""文档里提到 gemini-3.7-flash 不算。"""\n') == []


def test_src_has_no_model_ids() -> None:
    hits = [f"{p}:{ln}: {s!r}" for p in py_files() for ln, s in find_model_ids(p.read_text(encoding="utf-8"))]
    assert not hits, "型号应该写在 config/models.yaml：\n" + "\n".join(hits)
