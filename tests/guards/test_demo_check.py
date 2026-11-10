from tools.demo_check import CHECKS, run


def test_all_ten_steps_registered() -> None:
    assert sorted(CHECKS) == list(range(1, 11))


def test_nothing_opened_exits_zero() -> None:
    # D1：一个步骤都没打开，即使所有检查都是"未实现"，也返回 0（golden-demo.md 规则 1）
    assert run(force_all=True) == 0
