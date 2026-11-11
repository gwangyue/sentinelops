"""MCP 只读服务（02 §8 UC7）：只导出 mcp_export 的只读工具；调用方只能访问白名单里的目标；每次调用都写审计。"""

import asyncio
from typing import Any

from sentinel.core.packs_registry import load_pack
from sentinel.mcp.server import build_server, exported_tools, mcp_name


def server(calls: list[Any], audits: list[Any]):  # type: ignore[no-untyped-def]
    packs = {"es": load_pack("es"), "mysql": load_pack("mysql")}
    return build_server(
        exported_tools(packs),
        caller="workbuddy",
        allowed_targets={"es-target": "es"},
        call=lambda pack, tool, target, args: calls.append((pack, tool, target, args)) or ("ok", "status=green"),
        audit=lambda *a: audits.append(a),
    )


def test_only_exported_read_tools_are_listed() -> None:
    names = {t.name for t in asyncio.run(server([], []).list_tools())}
    assert mcp_name("es.cluster_health") in names and mcp_name("mysql.global_status") in names
    # 清单里标了 mcp_export: false 的不导出（可能含敏感内容）；动作一律不导出
    assert mcp_name("es.hot_threads") not in names and mcp_name("es.tasks") not in names
    assert mcp_name("mysql.processlist") not in names
    assert not any("raise" in n or "enable" in n or "set_replicas" in n for n in names)


def test_call_is_audited_and_target_whitelist_enforced() -> None:
    calls: list[Any] = []
    audits: list[Any] = []
    s = server(calls, audits)
    ok = asyncio.run(s.call_tool(mcp_name("es.cluster_health"), {"target": "es-target"}))
    denied = asyncio.run(s.call_tool(mcp_name("mysql.global_status"), {"target": "mysql-target"}))
    text = [c.text for r in (ok, denied) for c in r.content]  # type: ignore[union-attr]
    assert "[SentinelOps · es.cluster_health · es-target · ok]" in text[0] and "拒绝" in text[1]
    assert calls == [("es", "es.cluster_health", "es-target", {})]  # 被拒绝的那次根本没有调用工具
    assert audits == [
        ("workbuddy", "es.cluster_health", "es-target", "ok"),
        ("workbuddy", "mysql.global_status", "mysql-target", "denied"),
    ]
