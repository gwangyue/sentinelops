"""mysql.read_only：环境里登记为主库（env role=primary），但实例是只读的——所有写入都会失败。"""

from sentinel.contracts import Finding
from sentinel.sdk import DetectContext, make_finding

ID = "mysql.read_only"


def detect(ctx: DetectContext) -> list[Finding]:
    var = ctx.tools.call("mysql.global_variables")
    if not var.ok or ctx.env.get("role") != "primary" or str(var.raw.get("read_only")).upper() != "ON":
        return []
    return [
        make_finding(
            ctx,
            ID,
            severity="critical",
            summary="主库处于只读状态（read_only=ON），写入会失败",
            metrics={"read_only": 1.0},
            observation_ids=[var.observation_id],
        )
    ]
