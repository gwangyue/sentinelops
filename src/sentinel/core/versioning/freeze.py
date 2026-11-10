"""版本冻结（03 §5.2 规则 1–3；`sentinel version freeze`）。

负责：内容相同就复用已有版本；不同就新建（seq + 1，parent 指向上一个版本）；note 必填；
      型号不能是别名（G2）；影响行为的路径（ADR-012）有未提交的改动时，冻结和开始调查都拒绝。
不负责：组件从哪里来（components.py）。
"""

from datetime import UTC, datetime

from sentinel.contracts import AgentVersion, VersionComponents
from sentinel.core.store import Store
from sentinel.core.versioning.components import components_hash
from sentinel.core.versioning.models import assert_pinned


class FreezeError(RuntimeError):
    pass


def _refuse_dirty(dirty: list[str]) -> None:
    if dirty:
        shown = ", ".join(dirty[:5]) + (" …" if len(dirty) > 5 else "")
        raise FreezeError(f"影响行为的文件有未提交的改动（{shown}），先提交；否则版本对应不到一份确定的代码")


def freeze(
    store: Store, components: VersionComponents, note: str, *, dirty: list[str] | None = None
) -> tuple[AgentVersion, bool]:
    """返回 (版本, 是否新建)。"""
    if not note.strip():
        raise FreezeError("note 必填：说明这一版改了什么（03 §5.2 规则 3）")
    _refuse_dirty(dirty or [])
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


def require_frozen(store: Store, components: VersionComponents, *, dirty: list[str] | None = None) -> AgentVersion:
    """调查开始时调用：当前行为必须已经冻结过（03 §5.2 规则 2：没有版本哈希的结果不能写入）。

    有未提交的改动时也拒绝：实际运行的代码和 code_sha 对应的代码不一样，结果会挂到错误的版本上。
    """
    _refuse_dirty(dirty or [])
    v = store.get_version(components_hash(components))
    if v is None:
        raise FreezeError("当前的提示词 / 模型 / 工具 / 策略 / 代码还没有冻结成版本，先运行 sentinel version freeze")
    return v
