"""执行器的真实环境：写凭证、写连接、执行器自己用的只读工具（ADR-005；01 §6）。

负责：
- 写凭证：pack.yaml 的 connection.write_credential（es-executor）→ 环境变量 SENTINEL_CRED_ES_EXECUTOR_*；
  只有执行器进程会读这些变量（08 §6.4 的测试检查写凭证名只出现在 exec/）；
- WriteHttpClient：sdk.WriteHttp 的实现，只给 execute / rollback 用；
- ExecTools：重做前置检查用的只读调用。直接调专家包的 ReadTool.fetch，不经过调查引擎的 ToolBackend
  （执行器不依赖 core），也不写观测记录：这些读取是执行器的安全检查，不是诊断证据。
不负责：校验流程（core.py）。
"""

import os
from collections.abc import Mapping
from importlib.resources import files
from typing import Any

import httpx
import yaml

from sentinel.contracts import ActionSpec, PackManifest
from sentinel.sdk import Args, HttpError, ReadOnlyHttp, ReadTool, ToolResult, load_action_specs, resolve_entry


class ExecCredentialError(RuntimeError):
    pass


def manifest(pack: str) -> PackManifest:
    text = files(f"sentinel.packs.{pack}").joinpath("pack.yaml").read_text(encoding="utf-8")
    return PackManifest.model_validate(yaml.safe_load(text))


def write_connection(m: PackManifest) -> tuple[str, str, str]:
    name = m.connection.write_credential
    if not name:
        raise ExecCredentialError(f"专家包 {m.pack} 没有声明 write_credential")
    prefix = "SENTINEL_CRED_" + name.upper().replace("-", "_")
    try:
        return os.environ[f"{prefix}_URL"], os.environ[f"{prefix}_USER"], os.environ[f"{prefix}_PASSWORD"]
    except KeyError as e:
        raise ExecCredentialError(f"缺少环境变量 {e.args[0]}（执行器的写凭证，见 deploy/es/executor_role.sh）") from e


class WriteHttpClient:
    def __init__(self, url: str, user: str, password: str) -> None:
        # 和 ReadOnlyHttp 一样：kind 上是自签名证书。TODO(edge)：EKS 上挂 CA 证书并开启校验
        self._c = httpx.Client(base_url=url, auth=(user, password), verify=False, timeout=30)

    def _do(self, method: str, path: str, **kw: Any) -> Any:
        r = self._c.request(method, path, **kw)
        if r.status_code >= 400:
            raise HttpError(r.status_code, r.text)
        return r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text}

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return self._do("GET", path, params=params)

    def put(self, path: str, body: Mapping[str, Any]) -> Any:
        return self._do("PUT", path, json=dict(body))

    def post(self, path: str, body: Mapping[str, Any]) -> Any:
        return self._do("POST", path, json=dict(body))

    def close(self) -> None:
        self._c.close()


class ExecTools:
    """sdk.ToolCaller 的执行器版本：只读、不记录观测。"""

    def __init__(self, m: PackManifest, http: ReadOnlyHttp) -> None:
        self.tools: dict[str, ReadTool] = {t.id: resolve_entry(t.entry) for t in m.tools}
        self.http = http

    def call(self, tool: str, args: Args | None = None) -> ToolResult:
        t = self.tools[tool]
        a = {**t.defaults, **(args or {})}
        try:
            raw = t.fetch(self.http, a)
        except HttpError as e:
            return ToolResult(f"exec:{tool}", "error", str(e), str(e))
        return ToolResult(f"exec:{tool}", "ok", raw, t.summarize(raw, a))


def load_specs(m: PackManifest) -> dict[str, ActionSpec]:
    from pathlib import Path

    return load_action_specs(Path(str(files(f"sentinel.packs.{m.pack}"))), m.actions)
