"""agent-api 的 FastAPI 应用（ADR-002）。

负责：HTTP 入口。D3 有巡检（patrol CronJob 调用，02 §2）；审批接口在 M3 加。
不负责：业务逻辑，一律转给 core。
"""

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="sentinelops")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


class PatrolRequest(BaseModel):
    cluster: str
    pack: str = "es"


@app.post("/patrol")
def patrol(req: PatrolRequest) -> dict[str, object]:
    # 和 CLI 的 sentinel patrol 调用同一个函数。同步执行：巡检本身几秒钟（健康时不调用 LLM）
    from sentinel.core.graph.run import patrol as run_patrol

    s = run_patrol(req.cluster, req.pack)
    return {"investigation_id": s["inv_id"], "status": s["status"], "findings": len(s.get("findings", []))}
