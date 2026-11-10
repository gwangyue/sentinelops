"""只读 HTTP 连接（01 §6；ADR-005"诊断侧只有只读权限"）。

负责：对被管系统发 GET 请求，按 Content-Type 返回 JSON 或文本。
不负责：凭证从哪里来（core/credentials）、重试（TODO(edge)）。
这个类故意只有 get：专家包的工具拿到的连接对象上根本没有写方法，从结构上保证只读。
ES 的 allocation/explain 是"带请求体的 GET"，所以 get 允许传 body。
"""

from collections.abc import Mapping
from typing import Any

import httpx


class HttpError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status
        self.body = body


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
