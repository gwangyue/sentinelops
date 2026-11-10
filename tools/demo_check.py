"""`make demo-check`：golden-demo.md 的机器检查（G6）。

负责：按 tools/demo_steps.yaml 列出 10 个步骤，执行每一步的检查函数，打印状态表；
      只有"已打开"的步骤失败或未实现时才返回非零。
不负责：检查的具体内容——每个检查函数在对应功能完成的那天实现（D1 全部是"未实现"）。
检查只看契约和关系，不看具体数值（golden-demo.md 规则 3）。
"""

import argparse
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

import yaml

MILESTONES = ["none", "M1", "M2", "M3", "M4"]
STEPS_FILE = Path(__file__).with_name("demo_steps.yaml")


class Status(StrEnum):
    PASS = "通过"
    FAIL = "失败"
    TODO = "未实现"


def _not_implemented() -> tuple[Status, str]:
    return Status.TODO, ""


# 步骤编号 → 检查函数。实现某一步的那天，在这里换成真正的检查。
CHECKS: dict[int, Callable[[], tuple[Status, str]]] = {n: _not_implemented for n in range(1, 11)}


def run(force_all: bool) -> int:
    cfg = yaml.safe_load(STEPS_FILE.read_text(encoding="utf-8"))
    opened_idx = MILESTONES.index(cfg["opened_through"])
    bad = 0
    print(f"golden-demo 检查（已打开到：{cfg['opened_through']}）")
    for s in cfg["steps"]:
        opened = MILESTONES.index(s["open_at"]) <= opened_idx
        if not (opened or force_all):
            print(f"  {s['n']:>2}. {s['title']:<16} [未打开 · {s['open_at']}]")
            continue
        status, detail = CHECKS[s["n"]]()
        if opened and status is not Status.PASS:
            bad += 1
        tag = "已打开" if opened else f"未打开 · {s['open_at']}"
        print(f"  {s['n']:>2}. {s['title']:<16} [{tag}] {status} {detail}".rstrip())
    print(f"已打开的步骤中未通过：{bad}")
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true", help="未打开的步骤也执行检查，用来看进度")
    return run(ap.parse_args(argv).all)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
