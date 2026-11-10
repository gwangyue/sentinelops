"""凭证解析：pack.yaml 里的凭证名 → 连接信息（03 §3.1"凭证本身不写在清单里"）。

负责：只读凭证。名称 es-readonly 对应环境变量 SENTINEL_CRED_ES_READONLY_{URL,USER,PASSWORD}。
不负责：写凭证——只有 exec/ 能取（08 §6.4 有测试检查）；K8s 上改为挂 Secret（D3 的 Helm chart）。
"""

import os
from dataclasses import dataclass


class CredentialError(RuntimeError):
    pass


@dataclass(frozen=True)
class Connection:
    url: str
    user: str
    password: str


def resolve(name: str) -> Connection:
    prefix = "SENTINEL_CRED_" + name.upper().replace("-", "_")
    try:
        return Connection(os.environ[f"{prefix}_URL"], os.environ[f"{prefix}_USER"], os.environ[f"{prefix}_PASSWORD"])
    except KeyError as e:
        raise CredentialError(f"缺少环境变量 {e.args[0]}（本地可以 source deploy/kind/dev-env.sh）") from e
