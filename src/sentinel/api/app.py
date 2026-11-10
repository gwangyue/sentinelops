"""agent-api 的 FastAPI 应用（ADR-002）。

负责：HTTP 入口。D1 只有健康检查，调查和审批接口在 D3 和 M3 加。
不负责：业务逻辑，一律转给 core。
"""

from fastapi import FastAPI

app = FastAPI(title="sentinelops")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
