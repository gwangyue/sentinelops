"""MySQL 连接（M4 D13）：只读连接 ReadOnlySql 和写连接 WriteSql，由 connect.py 按 connection.kind=mysql 注册。

负责：建立 pymysql 连接；只读连接"从结构上只读"——类上只有 query，没有 execute / commit，
      并且会话设成 READ ONLY 事务（数据库层面再挡一次）；所有驱动错误包装成 ToolCallError。
不负责：凭证从哪里来（只读在 core/credentials，写只在 exec/）。
url 的格式：mysql://host:port（端口默认 3306）。
"""

from typing import Any
from urllib.parse import urlparse

from sentinel.sdk.http import ToolCallError


def _connect(url: str, user: str, password: str) -> Any:
    import pymysql  # 只有用到 MySQL 的进程才导入驱动
    from pymysql.cursors import DictCursor

    u = urlparse(url)
    try:
        return pymysql.connect(
            host=u.hostname or "127.0.0.1",
            port=u.port or 3306,
            user=user,
            password=password,
            cursorclass=DictCursor,
            connect_timeout=10,
            read_timeout=15,
            autocommit=True,
        )
    except pymysql.MySQLError as e:
        raise ToolCallError(e.args[0] if e.args and isinstance(e.args[0], int) else 0, str(e)) from e


class SqlError(ToolCallError):
    def __str__(self) -> str:
        return f"MySQL {self.status}: {self.body[:200]}"


def _run(conn: Any, sql: str) -> list[dict[str, Any]]:
    import pymysql

    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())
    except pymysql.MySQLError as e:
        raise SqlError(e.args[0] if e.args and isinstance(e.args[0], int) else 0, str(e)) from e


class ReadOnlySql:
    """只读连接：只有 query。第一次查询时才连接，会话设成只读事务（SET SESSION TRANSACTION READ ONLY）。"""

    def __init__(self, url: str, user: str, password: str) -> None:
        self._args = (url, user, password)
        self._c: Any = None

    def query(self, sql: str) -> list[dict[str, Any]]:
        if self._c is None:
            self._c = _connect(*self._args)
            _run(self._c, "SET SESSION TRANSACTION READ ONLY")
        return _run(self._c, sql)

    def close(self) -> None:
        if self._c is not None:
            self._c.close()
            self._c = None


class WriteSql:
    """写连接：只给执行器的 execute / rollback 用。执行器账号本身只有 SYSTEM_VARIABLES_ADMIN（01 §6）。"""

    def __init__(self, url: str, user: str, password: str) -> None:
        self._c = _connect(url, user, password)

    def execute(self, sql: str) -> dict[str, Any]:
        rows = _run(self._c, sql)
        return {"ok": True, "statement": sql.split()[0].upper(), "rows": len(rows)}

    def query(self, sql: str) -> list[dict[str, Any]]:
        return _run(self._c, sql)

    def close(self) -> None:
        self._c.close()
