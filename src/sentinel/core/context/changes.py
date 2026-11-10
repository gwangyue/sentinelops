"""L3 变更层（04 §3.4）："最近改了什么"往往就是根因。全部由确定性代码生成。

负责：把"上一次快照"和"现在"做结构化比较，得到变更列表，每条带快照时间和观测 ID。
      比较是通用的 JSON 结构比较，不依赖具体领域：字典按键比较；元素带 name 字段的列表按 name 比较。
      所以专家包不需要声明变更生成函数（PackManifest 不用改），快照哪些工具写在 context_policy 里。
不负责：快照从哪里来（SnapshotSource：回放时来自录制的 phase=before，线上来自 Postgres 的 snapshots 表）；
        应用发版事件（04 §3.4 只留接口，TODO(edge)）。
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

MISSING = object()


@dataclass(frozen=True)
class Snapshot:
    raw: Any
    ts: datetime


@dataclass(frozen=True)
class Change:
    tool: str
    path: str  # 例如 persistent.cluster.routing.allocation.enable
    before: str
    after: str
    since: datetime  # 上一次快照的时间：变更发生在这之后
    observation_id: str  # 现在的状态来自哪条观测记录

    def line(self) -> str:
        when = f"{self.since:%m-%d %H:%M} 之后"
        return f"{self.tool} {self.path}: {self.before} → {self.after}（{when}；{self.observation_id}）"


class SnapshotSource(Protocol):
    def previous(self, tool: str) -> Snapshot | None: ...


def _label(v: Any) -> str:
    if v is MISSING:
        return "（无）"
    if isinstance(v, dict):
        # 整个子树新增或消失时，用它的 name 字段代表它（例如一个节点），没有就截断显示
        return f"{{name={v['name']}}}" if "name" in v else json.dumps(v, ensure_ascii=False, sort_keys=True)[:80]
    return json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


def _keyed(v: Any) -> Any:
    # 元素都带 name 的列表（例如索引模板）按 name 比较，而不是按位置比较
    if isinstance(v, list) and v and all(isinstance(x, dict) and "name" in x for x in v):
        return {str(x["name"]): x for x in v}
    return v


def diff(before: Any, after: Any, path: str = "") -> list[tuple[str, str, str]]:
    """返回 (路径, 之前, 之后)。结构相同、值相同的部分不出现。"""
    before, after = _keyed(before), _keyed(after)
    if isinstance(before, dict) and isinstance(after, dict):
        out: list[tuple[str, str, str]] = []
        for k in sorted(set(before) | set(after), key=str):
            p = f"{path}.{k}" if path else str(k)
            b, a = before.get(k, MISSING), after.get(k, MISSING)
            if b is MISSING or a is MISSING:
                out.append((p, _label(b), _label(a)))
            else:
                out.extend(diff(b, a, p))
        return out
    if before != after:
        return [(path or "(root)", _label(before), _label(after))]
    return []


def collect_changes(
    current: Mapping[str, tuple[Any, str]],
    source: SnapshotSource,
    now: datetime,
    window_hours: float,
) -> list[Change]:
    """current：工具 → (现在的原始输出, 观测 ID)。超过窗口的旧快照不比较（04 §3.4"默认只看最近 24 小时"）。"""
    out: list[Change] = []
    for tool, (raw, obs_id) in current.items():
        prev = source.previous(tool)
        if prev is None or now - prev.ts > timedelta(hours=window_hours):
            continue  # TODO(edge): 没有快照（第一次巡检）时，在上下文里注明"变更层没有可比较的快照"
        out.extend(Change(tool, p, b, a, prev.ts, obs_id) for p, b, a in diff(prev.raw, raw))
    return out


class ReplaySnapshots:
    """回放时：上一次快照 = 录制里 phase=before 的记录（05 §3.5"基线也从录制文件里读取"）。"""

    def __init__(self, replay_before: Any) -> None:
        self._before = replay_before  # ReplayIndex.before

    def previous(self, tool: str) -> Snapshot | None:
        rec = self._before(tool, {})
        if rec is None or rec.get("raw") is None:
            return None
        return Snapshot(rec["raw"], datetime.fromisoformat(rec["ts"]))


class StoreSnapshots:
    """线上巡检时：上一次快照 = Postgres snapshots 表里这个集群最近一次巡检留下的。"""

    def __init__(self, store: Any, target: str) -> None:
        self._store, self._target = store, target

    def previous(self, tool: str) -> Snapshot | None:
        row = self._store.latest_snapshot(self._target, tool)
        return Snapshot(row[0], row[1]) if row else None
