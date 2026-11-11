"""只读 HTTP 连接（01 §6；ADR-005"诊断侧只有只读权限"）。

负责：对被管系统发 GET 请求，按 Content-Type 返回 JSON 或文本。
不负责：凭证从哪里来（core/credentials）、重试（TODO(edge)）。
这个类故意只有 get：专家包的工具拿到的连接对象上根本没有写方法，从结构上保证只读。
ES 的 allocation/explain 是"带请求体的 GET"，所以 get 允许传 body。
"""

from collections.abc import Mapping
from typing import Any

import httpx


class ToolCallError(RuntimeError):
    """连接层的调用失败（HTTP 4xx/5xx、SQL 错误……）：工具调用记为 error，不让异常冒出去。
    各种连接（connect.py）都把自己的错误包装成它，调查引擎和执行器只认这一个类型。"""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"{status}: {body[:200]}")
        self.status = status
        self.body = body


class HttpError(ToolCallError):
    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.body[:200]}"


class ReadOnlyHttp:
    def __init__(self, base_url: str, user: str, password: str, *, verify: bool = False, timeout: float = 15) -> None:
        # 本地 kind 上的 ES 用自签名证书，所以默认不校验。TODO(edge)：EKS 上改为挂 CA 证书并开启校验
        self._client = httpx.Client(base_url=base_url, auth=(user, password), verify=verify, timeout=timeout)

    def get(self, path: str, params: Mapping[str, Any] | None = None, body: Mapping[str, Any] | None = None) -> Any:
        r = self._client.request("GET", path, params=params, json=body)
        if r.status_code >= 400:
            raise HttpError(r.status_code, r.text)
        if r.headers.get("content-type", "").startswith("application/json"):
            return r.json()
        return r.text

    def close(self) -> None:
        self._client.close()


class WriteHttpClient:
    """sdk.WriteHttp 的实现：只给执行器的 execute / rollback 用。凭证由执行器（exec/）解析后传进来——
    这个类本身不碰凭证，所以放在 sdk 里，执行器和故障台按 connection.kind 通过 connect.py 取用。"""

    def __init__(self, base_url: str, user: str, password: str) -> None:
        # 和 ReadOnlyHttp 一样：kind 上是自签名证书。TODO(edge)：EKS 上挂 CA 证书并开启校验
        self._c = httpx.Client(base_url=base_url, auth=(user, password), verify=False, timeout=30)

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
