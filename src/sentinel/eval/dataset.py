"""数据集：场景定义、git 里的用例文件、split 分配、同步到 LangSmith 并打版本标签（06 §5；03 §6.2）。

负责：datasets/<pack>.jsonl 是唯一的真实来源；LangSmith 是它的同步副本。
不负责：录制（bench）、评测运行（runner）。
"""

import hashlib
import importlib.resources
import json
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

from sentinel.contracts import EvalExample, ScenarioSpec

REPO = Path(__file__).resolve().parents[3]
DATASETS = REPO / "datasets"
FIXTURES = REPO / "fixtures"
# 用例 ID → LangSmith 的 example UUID：固定的命名空间，重复同步得到同一个 UUID，不会重复上传
_NS = uuid.UUID("5e6f1c2a-7d1b-4c3e-9a40-3b1f0e9d2c11")


def split_for(scenario_id: str, origin: str = "bench") -> Literal["dev", "heldout"]:
    """06 §5.2：外部改编的场景一律进 heldout（G1）；其余按 hash(scenario_id) mod 4 == 0 → heldout。

    按场景分配，同一场景的所有录制在同一个 split。规则写在代码里，不靠起名字来凑比例。
    """
    if origin == "external_adapted":
        return "heldout"
    return "heldout" if int(hashlib.sha256(scenario_id.encode()).hexdigest(), 16) % 4 == 0 else "dev"


def example_uuid(example_id: str) -> str:
    return str(uuid.uuid5(_NS, example_id))


def load_scenarios(pack: str) -> dict[str, ScenarioSpec]:
    root = importlib.resources.files(f"sentinel.packs.{pack}").joinpath("scenarios")
    out = {}
    for f in sorted(root.iterdir(), key=lambda p: p.name):
        if f.name.endswith(".yaml"):
            s = ScenarioSpec.model_validate(yaml.safe_load(f.read_text(encoding="utf-8")))
            out[s.id] = s
    return out


def dataset_file(pack: str) -> Path:
    return DATASETS / f"{pack}.jsonl"


def load_examples(pack: str) -> list[EvalExample]:
    p = dataset_file(pack)
    if not p.exists():
        return []
    return [EvalExample.model_validate_json(line) for line in p.read_text(encoding="utf-8").splitlines() if line]


def append_example(pack: str, ex: EvalExample) -> None:
    DATASETS.mkdir(exist_ok=True)
    with dataset_file(pack).open("a", encoding="utf-8") as f:
        f.write(ex.model_dump_json() + "\n")


def next_recording(pack: str, scenario_id: str) -> int:
    """按录制历史（fixtures/index.jsonl）编号，而不是按当前数据集：
    从数据集里撤下的旧录制文件仍然保留（旧的数据集标签要能复现），新录制不能覆盖它们。"""
    seen = [r["example_id"] for r in read_fixture_index() if r["example_id"].startswith(f"{scenario_id}#r")]
    seen += [e.id for e in load_examples(pack) if e.scenario_id == scenario_id]
    return max((int(x.rsplit("#r", 1)[1]) for x in seen), default=0) + 1


# ---------- LangSmith 同步 ----------


class LangSmithLike(Protocol):
    """测试里用假的实现；真实的是 langsmith.Client。"""

    def has_dataset(self, *, dataset_name: str) -> bool: ...
    def create_dataset(self, dataset_name: str, *, description: str) -> Any: ...
    def read_dataset(self, *, dataset_name: str) -> Any: ...
    def list_examples(self, *, dataset_id: Any) -> Any: ...
    def create_examples(self, *, dataset_id: Any, examples: list[dict[str, Any]]) -> Any: ...
    def update_examples(self, *, dataset_id: Any, updates: list[dict[str, Any]]) -> Any: ...
    def delete_examples(self, example_ids: list[str]) -> Any: ...
    def list_dataset_versions(self, *, dataset_id: Any) -> Any: ...
    def update_dataset_tag(self, *, dataset_id: Any, as_of: datetime, tag: str) -> None: ...


def to_langsmith(ex: EvalExample) -> dict[str, Any]:
    return {
        "id": example_uuid(ex.id),
        "inputs": {"example_id": ex.id, "scenario_id": ex.scenario_id, **ex.inputs.model_dump(mode="json")},
        "outputs": ex.outputs.model_dump(mode="json"),
        "metadata": {"example_id": ex.id, "scenario_id": ex.scenario_id, **ex.metadata.model_dump(mode="json")},
        "split": ex.split,
    }


def next_tag(changelog: Path, today: str) -> str:
    n = changelog.read_text(encoding="utf-8").count(f"ds-{today}-") if changelog.exists() else 0
    return f"ds-{today}-{n + 1}"


def sync(pack: str, note: str, client: LangSmithLike) -> tuple[str, dict[str, int]]:
    """upsert 全部用例，打标签，写变更日志。返回 (标签, 各 split 的用例数)。"""
    if not note.strip():
        raise ValueError("note 必填：这次数据集变化做了什么、为什么（06 §5.2 变更日志）")
    examples = load_examples(pack)
    name = f"sentinel-{pack}"
    ds = (
        client.read_dataset(dataset_name=name)
        if client.has_dataset(dataset_name=name)
        else client.create_dataset(
            name, description=f"SentinelOps {pack} 评测集；以 git 的 datasets/{pack}.jsonl 为准，只读副本"
        )
    )
    existing = {str(e.id) for e in client.list_examples(dataset_id=ds.id)}
    rows = [to_langsmith(e) for e in examples]
    new = [r for r in rows if r["id"] not in existing]
    old = [r for r in rows if r["id"] in existing]
    if new:
        client.create_examples(dataset_id=ds.id, examples=new)
    if old:
        client.update_examples(dataset_id=ds.id, updates=old)
    # git 里撤下的用例，LangSmith 上也删掉（新版本里不再包含）；旧标签仍然指向旧版本，所以旧结果照样能复现
    gone = sorted(existing - {r["id"] for r in rows})
    if gone:
        client.delete_examples(gone)
    time.sleep(2)  # 新版本生成需要一点时间（S4）
    # 标签打在最新版本的时间上；不能用 dataset.modified_at，它不随添加用例前进（S4 的坑）
    as_of = max(v.as_of for v in client.list_dataset_versions(dataset_id=ds.id))
    changelog = DATASETS / "CHANGELOG.md"
    tag = next_tag(changelog, datetime.now(UTC).strftime("%Y%m%d"))
    client.update_dataset_tag(dataset_id=ds.id, as_of=as_of, tag=tag)
    counts = {s: sum(1 for e in examples if e.split == s) for s in ("dev", "heldout")}
    with changelog.open("a", encoding="utf-8") as f:
        f.write(
            f"| {tag} | {pack} | {len(examples)} | dev {counts['dev']} / heldout {counts['heldout']} "
            f"| 新增 {len(new)}，删除 {len(gone)} | {note.strip()} |\n"
        )
    return tag, counts


def write_fixture_index(entry: dict[str, Any]) -> None:
    """fixtures/index.jsonl 进 git：原始录制不进 git（01 §4），但每条录制的来历和哈希要留下。"""
    FIXTURES.mkdir(exist_ok=True)
    with (FIXTURES / "index.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_fixture_index() -> list[dict[str, Any]]:
    p = FIXTURES / "index.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x] if p.exists() else []
