import yaml
from tools.demo_check import CHECKS, STEPS_FILE, run


def test_all_steps_registered() -> None:
    # 和 demo_steps.yaml 对齐，而不是写死个数：M5 加了第 11 步（09 篇），以后加步骤不用再改这里
    steps = yaml.safe_load(STEPS_FILE.read_text(encoding="utf-8"))["steps"]
    assert sorted(CHECKS) == [s["n"] for s in steps]


def test_nothing_opened_exits_zero() -> None:
    # D1：一个步骤都没打开，即使所有检查都是"未实现"，也返回 0（golden-demo.md 规则 1）
    assert run(force_all=True) == 0
