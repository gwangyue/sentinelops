"""观测记录存储（03 §3.3：证据链的根）。

负责：原始输出写成文件（raw_ref），观测记录写进索引；读取时校验 sha256。
      索引有两种：调查图运行时写进 Store（Postgres，D3）；开发命令（sentinel detect / tools call）
      没有数据库时写进每次调查一份的 JSONL 文件。
不负责：S3 上的大文件存储（M4）。
"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from ulid import ULID

from sentinel.contracts import Observation


class TamperedObservationError(RuntimeError):
    pass


class ObservationIndex(Protocol):
    def add_observation(self, obs: Observation) -> None: ...
    def observations(self, investigation_id: str) -> list[Observation]: ...


class FileIndex:
    """每次调查一份 index.jsonl，放在原始输出旁边。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def add_observation(self, obs: Observation) -> None:
        p = self.root / obs.investigation_id / "index.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(obs.model_dump_json() + "\n")

    def observations(self, investigation_id: str) -> list[Observation]:
        p = self.root / investigation_id / "index.jsonl"
        if not p.exists():
            return []
        return [Observation.model_validate_json(line) for line in p.read_text(encoding="utf-8").splitlines()]


def _raw_bytes(raw: Any) -> bytes:
    return json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()


class ObservationStore:
    def __init__(self, root: Path, index: ObservationIndex | None = None) -> None:
        self.root = root
        self.index = index or FileIndex(root)

    @staticmethod
    def new_id() -> str:
        # 先分配 ID 再写入：摘要被截断时，截断说明里要引用这条观测的 ID
        return f"obs_{ULID()}"

    def put(
        self,
        investigation_id: str,
        tool: str,
        args: dict[str, Any],
        backend: Literal["live", "record", "replay"],
        status: Literal["ok", "error", "replay_miss"],
        raw: Any,
        digest: str,
        obs_id: str | None = None,
    ) -> Observation:
        obs_id = obs_id or self.new_id()
        data = _raw_bytes(raw)
        path = self.root / investigation_id / f"{obs_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        obs = Observation(
            id=obs_id,
            investigation_id=investigation_id,
            tool=tool,
            args=args,
            backend=backend,
            status=status,
            raw_ref=str(path),
            raw_sha256=hashlib.sha256(data).hexdigest(),
            digest=digest,
            created_at=datetime.now(UTC),
        )
        self.index.add_observation(obs)
        return obs

    def list(self, investigation_id: str) -> list[Observation]:
        return self.index.observations(investigation_id)

    def get(self, investigation_id: str, obs_id: str) -> tuple[Observation, Any]:
        """返回观测记录和原始输出。原始文件被改过就报错：证据链不能建立在被篡改的数据上。"""
        obs = next((o for o in self.list(investigation_id) if o.id == obs_id), None)
        if obs is None:
            raise KeyError(obs_id)
        data = Path(obs.raw_ref).read_bytes()
        if hashlib.sha256(data).hexdigest() != obs.raw_sha256:
            raise TamperedObservationError(f"{obs_id} 的原始输出和记录的 sha256 不一致")
        return obs, json.loads(data)
