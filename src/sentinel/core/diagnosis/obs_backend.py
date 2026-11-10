"""把本次调查的观测记录挂成只读虚拟文件 /obs/<观测 ID>.json（04 §3.3 分段读取；S3 验证过 CompositeBackend 的路由）。"""

import json
from typing import Any

from deepagents.backends.protocol import BackendProtocol, GlobResult, GrepResult, LsResult, ReadResult
from deepagents.backends.utils import normalize_read_bounds

from sentinel.core.observations.store import ObservationStore


class ObsBackend(BackendProtocol):
    def __init__(self, store: ObservationStore, investigation_id: str) -> None:
        self.store, self.investigation_id = store, investigation_id

    def _id(self, path: str) -> str:
        # CompositeBackend 会去掉路由前缀再传进来，两种写法都接受
        return path.removeprefix("/obs/").lstrip("/").removesuffix(".json")

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        try:
            obs, raw = self.store.get(self.investigation_id, self._id(file_path))
        except KeyError:
            return ReadResult(error=f"not found: {file_path}")
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, indent=1)
        offset, limit = normalize_read_bounds(offset, limit)
        lines = text.splitlines(keepends=True)
        window = lines[offset : offset + limit]
        # 原始输出同样来自被管系统，属于不可信内容（04 §3.8）
        content = f'<untrusted source="{obs.id}">\n' + "".join(window) + "\n</untrusted>\n"
        return ReadResult(
            file_data={"content": content, "encoding": "utf-8"},
            total_lines=len(lines),
            start_line=offset + 1,
            end_line=offset + len(window),
        )

    def ls(self, path: str) -> LsResult:
        return LsResult(
            entries=[{"path": f"/obs/{o.id}.json", "is_dir": False} for o in self.store.list(self.investigation_id)]
        )

    def grep(
        self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None
    ) -> GrepResult:
        return GrepResult(matches=[])  # TODO(edge): 观测内容的 grep

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return GlobResult(matches=[{"path": f"/obs/{o.id}.json"} for o in self.store.list(self.investigation_id)])

    # 只读：观测记录由确定性代码写入，Agent 不能改
    def write(self, *a: Any, **kw: Any) -> Any:
        raise PermissionError("/obs/ is read-only")

    def edit(self, *a: Any, **kw: Any) -> Any:
        raise PermissionError("/obs/ is read-only")
