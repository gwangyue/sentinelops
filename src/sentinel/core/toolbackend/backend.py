"""ToolBackend：工具调用的三种模式 Live / Record / Replay（01 §3.1；ADR-003）。

负责：执行一次工具调用 → 原始输出 → 确定性摘要（受输出预算约束）→ 写入观测存储 → 返回 ToolResult。
      Record 模式把原始输出追加进录制文件；Replay 模式只从录制文件里取，取不到就记 replay_miss。
      同一次调查里，同一个（工具，规范化参数）只真正执行一次（检测器和 fact builder 会重复查同一个接口）。
不负责：工具本身怎么取数据、怎么摘要（专家包）；回放覆盖率统计（评测运行器，D4）。
"""

import json
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from sentinel.core.credentials import resolve
from sentinel.core.observations.store import ObservationStore
from sentinel.core.packs_registry import LoadedPack, LoadedTool
from sentinel.core.toolbackend.canonical import canonical_args, replay_key
from sentinel.sdk import HttpError, ReadOnlyHttp, ToolResult, fit_budget


class Mode(StrEnum):
    LIVE = "live"
    RECORD = "record"
    REPLAY = "replay"


class ReplayIndex:
    """录制文件：每行 {tool, args, raw, error, phase, ts}。同一个键录到多次时按顺序返回，用完后一直返回最后一次。

    phase：before = 注入故障之前的快照（L3 变更层的"上一次快照"，05 §3.5"回放时基线也从录制里读"）；
           pre = 故障状态（工具调用回放的就是这些）；post = 执行动作之后（M3）。
    """

    def __init__(self, path: Path) -> None:
        self._items: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._before: dict[str, dict[str, Any]] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            key = replay_key(rec["tool"], rec["args"])
            phase = rec.get("phase", "pre")
            if phase == "pre":
                self._items[key].append(rec)
            elif phase == "before":
                self._before[key] = rec
            # TODO(edge): phase="post"（执行动作之后的观测，M3）要按执行进度切换

    def before(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """注入之前的快照记录；旧录制没有快照时返回 None。"""
        return self._before.get(replay_key(tool, args))

    def take(self, key: str) -> dict[str, Any] | None:
        q = self._items.get(key)
        if not q:
            return None
        return q.popleft() if len(q) > 1 else q[0]


@dataclass
class _Fetched:
    status: Literal["ok", "error", "replay_miss"]
    raw: Any


class ToolBackend:
    """实现 sdk.ToolCaller。一个实例对应一次调查。"""

    def __init__(
        self,
        tools: Mapping[str, LoadedTool],
        store: ObservationStore,
        investigation_id: str,
        mode: Mode,
        *,
        http: Callable[[], ReadOnlyHttp] | None = None,
        fixture: Path | None = None,
        phase: str = "pre",
    ) -> None:
        if mode is not Mode.REPLAY and http is None:
            raise ValueError("live / record 模式需要连接")
        if mode is not Mode.LIVE and fixture is None:
            raise ValueError("record / replay 模式需要录制文件路径")
        self.tools, self.store, self.investigation_id, self.mode = tools, store, investigation_id, mode
        self._http_factory = http
        self._http: ReadOnlyHttp | None = None
        self._fixture = fixture
        self._replay = ReplayIndex(fixture) if mode is Mode.REPLAY and fixture else None
        self._memo: dict[str, ToolResult] = {}
        self.phase = phase  # 只影响 Record 模式写进录制的 phase 字段

    def replay_before(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """回放模式下，录制里注入前的快照（L3 变更层用）；非回放模式返回 None。"""
        return self._replay.before(tool, args) if self._replay is not None else None

    def call(self, tool: str, args: Mapping[str, Any] | None = None) -> ToolResult:
        spec = self.tools.get(tool)
        if spec is None:
            raise KeyError(f"未注册的工具 {tool}")
        cargs = canonical_args(args, spec.impl.defaults)
        key = replay_key(tool, cargs)
        if key in self._memo:
            return self._memo[key]
        fetched = self._fetch(spec, tool, cargs, key)
        if fetched.status == "ok":
            digest = spec.impl.summarize(fetched.raw, {**spec.impl.defaults, **cargs})
        elif fetched.status == "replay_miss":
            digest = "录制未覆盖这次调用（replay_miss），没有数据"
        else:
            digest = f"调用失败：{fetched.raw}"
        obs_id = self.store.new_id()
        digest = fit_budget(digest, spec.decl.output_budget_tokens, f"…（已截断，完整内容见 /obs/{obs_id}.json）")
        self.store.put(
            self.investigation_id, tool, cargs, self.mode.value, fetched.status, fetched.raw, digest, obs_id=obs_id
        )
        result = ToolResult(observation_id=obs_id, status=fetched.status, raw=fetched.raw, digest=digest)
        self._memo[key] = result
        return result

    def _fetch(self, spec: LoadedTool, tool: str, cargs: dict[str, Any], key: str) -> _Fetched:
        if self._replay is not None:
            rec = self._replay.take(key)
            if rec is None:
                return _Fetched("replay_miss", None)  # 不访问集群，也不模糊匹配（ADR-003）
            return _Fetched("error", rec["error"]) if rec.get("error") else _Fetched("ok", rec["raw"])
        if self._http is None:
            assert self._http_factory is not None
            self._http = self._http_factory()
        try:
            fetched = _Fetched("ok", spec.impl.fetch(self._http, {**spec.impl.defaults, **cargs}))
        except HttpError as e:
            fetched = _Fetched("error", {"status": e.status, "body": e.body[:2000]})
        if self.mode is Mode.RECORD:
            assert self._fixture is not None
            self._fixture.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "tool": tool,
                "args": cargs,
                "phase": self.phase,
                "ts": datetime.now(UTC).isoformat(),  # L3 变更层的窗口判断用快照时间
                "raw": fetched.raw if fetched.status == "ok" else None,
                "error": fetched.raw if fetched.status == "error" else None,
            }
            with self._fixture.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return fetched

    def close(self) -> None:
        if self._http is not None:
            self._http.close()


def open_backend(
    pack: LoadedPack,
    investigation_id: str,
    mode: Mode,
    store: ObservationStore,
    fixture: Path | None = None,
) -> ToolBackend:
    """按专家包声明的只读凭证建立连接（凭证名 → 环境变量，见 core/credentials）。回放模式不建立连接。"""
    http = None
    if mode is not Mode.REPLAY:
        conn = resolve(pack.manifest.connection.read_credential)
        http = lambda: ReadOnlyHttp(conn.url, conn.user, conn.password)  # noqa: E731
    return ToolBackend(pack.tools, store, investigation_id, mode, http=http, fixture=fixture)
