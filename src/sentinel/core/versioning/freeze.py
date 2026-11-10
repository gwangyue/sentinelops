"""版本冻结（03 §5.2 规则 1–3；`sentinel version freeze`）。

负责：内容相同就复用已有版本；不同就新建（seq + 1，parent 指向上一个版本）；note 必填；
      型号不能是别名（G2）；工作区有未提交的改动时拒绝冻结（code_sha 会对不上实际代码）。
不负责：组件从哪里来（components.py）。
"""

from datetime import UTC, datetime

from sentinel.contracts import AgentVersion, VersionComponents
from sentinel.core.store import Store
from sentinel.core.versioning.components import components_hash
from sentinel.core.versioning.models import assert_pinned


class FreezeError(RuntimeError):
    pass


def freeze(store: Store, components: VersionComponents, note: str, *, dirty: bool = False) -> tuple[AgentVersion, bool]:
    """返回 (版本, 是否新建)。"""
    if not note.strip():
        raise FreezeError("note 必填：说明这一版改了什么（03 §5.2 规则 3）")
    if dirty:
        raise FreezeError("工作区有未提交的改动，先提交再冻结，否则 code_sha 对不上实际代码")
    for m in components.models.values():
        assert_pinned(str(m.get("model", "")))  # 别名直接抛 ModelAliasError
    h = components_hash(components)
    existing = store.get_version(h)
    if existing is not None:
        return existing, False
    latest = store.latest_version()
    v = AgentVersion(
        seq=(latest.seq + 1) if latest else 1,
        hash=h,
        parent=latest.hash if latest else None,
        created_at=datetime.now(UTC),
        note=note.strip(),
        components=components,
    )
    store.insert_version(v)
    return v, True


def require_frozen(store: Store, components: VersionComponents) -> AgentVersion:
    """调查开始时调用：当前行为必须已经冻结过（03 §5.2 规则 2：没有版本哈希的结果不能写入）。"""
    v = store.get_version(components_hash(components))
    if v is None:
        raise FreezeError("当前的提示词 / 模型 / 工具 / 策略 / 代码还没有冻结成版本，先运行 sentinel version freeze")
    return v
