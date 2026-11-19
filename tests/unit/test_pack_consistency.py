"""专家包一致性（03 §3.1、§4.2、§6.1；ADR-011）：对 sentinel/packs/ 下的每一个专家包参数化，新增领域自动覆盖。

为什么要有：load_pack / sentinel pack check 只校验清单、解析工具 / 检测器 / fact builder 的入口。
下面这几类错误加载时不报（教程 03 §3.13 用玩具包实测），要到运行时才暴露，有的永远不报：
动作规格里的函数名拼错、connection.kind 没注册、根因表缺 <领域>.unknown、检测器 id 和代码里的 ID 不一致、
场景引用了不存在的检测器 / 根因 / 动作。离线：只读清单、规格和场景，不访问网络。
"""

import importlib

import pytest

from sentinel.core.packs_registry import LoadedPack, discover_packs, load_pack
from sentinel.eval.dataset import load_scenarios
from sentinel.sdk import CONNECTORS, resolve_entry

PACKS = discover_packs()


@pytest.fixture(scope="module", params=PACKS)
def pack(request: pytest.FixtureRequest) -> LoadedPack:
    return load_pack(request.param)


def test_discovers_es_and_mysql() -> None:
    # 防止目录扫描坏掉之后，下面的参数化测试悄悄变成 0 个
    assert {"es", "mysql"} <= set(PACKS)


def test_connection_kind_registered(pack: LoadedPack) -> None:
    # 没注册时，回放模式渲染诊断提示词也会 KeyError（领域显示名从连接器取）
    assert pack.manifest.connection.kind in CONNECTORS


def test_ids_use_pack_namespace(pack: LoadedPack) -> None:
    name = pack.manifest.pack
    ids = [
        *pack.tools,
        *pack.detectors,
        *pack.actions,
        *(r.label for r in pack.manifest.root_causes),
    ]
    assert [i for i in ids if not i.startswith(f"{name}.")] == []
    assert {a.pack for a in pack.actions.values()} <= {name}


def test_root_causes_include_unknown(pack: LoadedPack) -> None:
    # 缺了它，agent.render_prompt 会兜底成 es.unknown（ES 时代的常量），模型照选会被归一化成 None
    labels = [r.label for r in pack.manifest.root_causes]
    assert f"{pack.manifest.pack}.unknown" in labels
    assert len(labels) == len(set(labels))


def test_action_entries_resolve(pack: LoadedPack) -> None:
    # load_pack 只读规格不解析函数（不在调查进程里导入执行函数）；拼错的入口要到执行 / 验证时才 AttributeError
    for spec in pack.actions.values():
        entries = [*spec.preconditions, spec.dry_run, spec.execute, spec.verify.predicate]
        if spec.rollback:
            entries.append(spec.rollback)
        for e in entries:
            assert callable(resolve_entry(e)), f"{spec.id}: {e}"
        assert spec.reversible == (spec.rollback is not None), spec.id


def test_detector_id_matches_code(pack: LoadedPack) -> None:
    # 约定：检测器模块的 ID 常量就是 make_finding 用的 detector；和清单不一致时没有任何代码会发现
    for d in pack.manifest.detectors:
        module = importlib.import_module(d.entry.partition(":")[0])
        assert getattr(module, "ID", None) == d.id, d.entry


def test_scenarios_reference_existing_pieces(pack: LoadedPack) -> None:
    name = pack.manifest.pack
    labels = {r.label for r in pack.manifest.root_causes}
    scenarios = load_scenarios(name)
    assert scenarios, f"{name} 没有场景"
    for sid, s in scenarios.items():
        gt = s.ground_truth
        assert s.pack == name and sid.startswith(f"{name}."), sid
        assert gt.root_cause_label in labels, sid
        assert s.symptom_wait.detector in pack.detectors, sid  # 故障台按它等症状
        assert set(gt.acceptable_actions) <= set(pack.actions), sid
        assert set(gt.must_cite_tools) <= set(pack.tools), sid
        # forbidden_actions 可以不在动作目录里（例如 es.delete_index）：它们本来就不该被提议
        if s.remediation is not None:
            assert s.remediation.action_id in gt.acceptable_actions, sid
