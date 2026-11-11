"""CLI 的命令都注册上了（M2 D9：一次补丁误删了 knowledge index 的装饰器，make check 没发现）。"""

import typer.main

from sentinel.cli.main import app

EXPECTED = {
    ("knowledge", "index"),
    ("eval", "run"),
    ("eval", "baseline"),
    ("eval", "compare"),
    ("eval", "stream"),
    ("eval", "clusters"),
    ("eval", "curve"),
    ("eval", "gate"),
    ("eval", "ablation", "memory"),
    ("dataset", "sync"),
    ("dataset", "streams"),
    ("investigations", "context"),
    ("investigations", "confirm"),
    ("version", "freeze"),
    ("approvals", "list"),
    ("audit", "verify"),
    ("audit", "anchor"),
    ("freeze",),
    ("report", "weekly"),
    ("unfreeze",),
    ("approvals", "show"),
    ("approvals", "approve"),
    ("approvals", "reject"),
}


def _paths(cmd: object, prefix: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    subs = getattr(cmd, "commands", None)
    if not subs:
        return {prefix}
    out: set[tuple[str, ...]] = set()
    for name, c in subs.items():
        out |= _paths(c, (*prefix, name))
    return out


def test_expected_commands_registered() -> None:
    got = _paths(typer.main.get_command(app))
    missing = EXPECTED - got
    assert not missing, f"没有注册的命令：{sorted(missing)}"
