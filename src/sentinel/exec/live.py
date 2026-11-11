"""执行器的真实环境：写凭证、写连接、执行器自己用的只读工具（ADR-005；01 §6）。

负责：
- 写凭证：pack.yaml 的 connection.write_credential（es-executor）→ 环境变量 SENTINEL_CRED_ES_EXECUTOR_*；
  只有执行器进程会读这些变量（08 §6.4 的测试检查写凭证名只出现在 exec/）；
- 写连接按 connection.kind 由 sdk.open_write 构造（ES 是 sdk 里的 WriteHttpClient），只给 execute / rollback 用；
- ExecTools：重做前置检查用的只读调用。直接调专家包的 ReadTool.fetch，不经过调查引擎的 ToolBackend
  （执行器不依赖 core），也不写观测记录：这些读取是执行器的安全检查，不是诊断证据。
不负责：校验流程（core.py）。
"""

import os
from importlib.resources import files
from typing import Any

import yaml

from sentinel.contracts import ActionSpec, PackManifest
from sentinel.sdk import Args, ReadTool, ToolCallError, ToolResult, load_action_specs, resolve_entry


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


class ExecTools:
    """sdk.ToolCaller 的执行器版本：只读、不记录观测。"""

    def __init__(self, m: PackManifest, conn: Any) -> None:
        self.tools: dict[str, ReadTool] = {t.id: resolve_entry(t.entry) for t in m.tools}
        self.conn = conn  # 只读连接（sdk.open_read）

    def call(self, tool: str, args: Args | None = None) -> ToolResult:
        t = self.tools[tool]
        a = {**t.defaults, **(args or {})}
        try:
            raw = t.fetch(self.conn, a)
        except ToolCallError as e:
            return ToolResult(f"exec:{tool}", "error", str(e), str(e))
        return ToolResult(f"exec:{tool}", "ok", raw, t.summarize(raw, a))


def load_specs(m: PackManifest) -> dict[str, ActionSpec]:
    from pathlib import Path

    return load_action_specs(Path(str(files(f"sentinel.packs.{m.pack}"))), m.actions)


def discover_packs() -> list[str]:
    """和 core.packs_registry.discover_packs 同样的规则（执行器不能依赖 core，所以各写一份，只有几行）。"""
    root = files("sentinel.packs")
    return sorted(d.name for d in root.iterdir() if d.is_dir() and d.joinpath("pack.yaml").is_file())
