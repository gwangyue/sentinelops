"""连接注册表（01 §4；M4 D13 通用化）：connection.kind → 只读连接、写连接、领域显示名。

负责：按专家包声明的 connection.kind 构造连接。调查引擎（core/toolbackend）和执行器（exec）都只调用
      open_read / open_write，不认识具体的连接类型——新增一个领域（例如 MySQL）只改 sdk 和专家包。
不负责：凭证从哪里来（只读凭证在 core/credentials，写凭证只在 exec/，08 §6.4）。
只读连接"从结构上只读"：只读连接类上根本没有写方法（tests 检查）。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sentinel.sdk.http import ReadOnlyHttp, WriteHttpClient
from sentinel.sdk.sql import ReadOnlySql, WriteSql


@dataclass(frozen=True)
class Connector:
    display: str  # 领域的显示名：提示词里"你是一名 {display} 运维专家"
    read: Callable[[str, str, str], Any]  # (url, user, password) → 只读连接
    write: Callable[[str, str, str], Any]  # (url, user, password) → 写连接（只有执行器会调用）


CONNECTORS: dict[str, Connector] = {
    "elasticsearch": Connector("Elasticsearch", ReadOnlyHttp, WriteHttpClient),
    "mysql": Connector("MySQL", ReadOnlySql, WriteSql),  # M4 D13：新增领域只改这里和专家包
}


def connector(kind: str) -> Connector:
    if kind not in CONNECTORS:
        raise KeyError(f"不认识的连接类型 {kind}：在 sdk/connect.py 的 CONNECTORS 里注册")
    return CONNECTORS[kind]


def open_read(kind: str, url: str, user: str, password: str) -> Any:
    return connector(kind).read(url, user, password)


def open_write(kind: str, url: str, user: str, password: str) -> Any:
    return connector(kind).write(url, user, password)


def domain_name(kind: str) -> str:
    return connector(kind).display
