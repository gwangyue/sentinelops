from tools.checks.interrupt_lint import check_source

BAD_BARE = """
try:
    interrupt({"plan": 1})
except:
    pass
"""
BAD_EXC = """
try:
    x = langgraph.types.interrupt(1)
except (ValueError, Exception):
    pass
"""
OK_NARROW = """
try:
    interrupt(1)
except ValueError:
    pass
"""
OK_OUTSIDE = """
v = interrupt(1)
try:
    parse(v)
except Exception:
    pass
"""


def test_flags_broad_except() -> None:
    assert check_source(BAD_BARE)
    assert check_source(BAD_EXC)


def test_allows_narrow_or_outside() -> None:
    assert check_source(OK_NARROW) == []
    assert check_source(OK_OUTSIDE) == []
