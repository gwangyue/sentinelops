"""ChatOps 配置（09 §6）：每个环境一份 yaml，用 SENTINEL_CHATOPS_CONFIG 选择。

负责：读配置、读凭证文件（--env-file）、算出"飞书上最高能审批到哪个风险等级"。
不负责：凭证本身放在哪里——dev 在本机文件里，prod 由 k8s secret 注入为环境变量（09 §7）。
"""

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

Risk = Literal["low", "medium", "high"]
_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "config" / "chatops.dev.yaml"


class ApprovalPolicy(BaseModel):
    max_risk_via_chat: Risk = "medium"  # 09 §2 D3：默认 medium，跑通之后再放开


class ChatopsConfig(BaseModel):
    env_label: str  # 卡片标题上的环境标签，例：kind-dev / eks-prod（09 §7）
    domain: str = "https://open.feishu.cn"  # 国内飞书；Lark 国际版是 https://open.larksuite.com（S7）
    chat_id: str | None = None  # 只推送、只接收这个群；不写就读环境变量 FEISHU_CHAT_ID
    approvers: dict[str, str] = Field(default_factory=dict)  # open_id → 名字（D19 用；open_id 按应用区分）
    approval: ApprovalPolicy = Field(default_factory=ApprovalPolicy)
    # 09 §2 D4：一个开关同时控制"飞书能审批 high"和"飞书能 /freeze"
    high_risk_enabled: bool = False
    commands: list[str] = Field(default_factory=list)  # D20 的指令白名单
    poll_interval_s: float = 5.0  # notifier 读审计表的间隔

    @property
    def max_risk(self) -> Risk:
        return "high" if self.high_risk_enabled else self.approval.max_risk_via_chat

    def resolved_chat_id(self) -> str:
        cid = self.chat_id or os.environ.get("FEISHU_CHAT_ID")
        if not cid:
            raise ValueError("没有配置群：在 chatops 配置里写 chat_id，或者设置 FEISHU_CHAT_ID")
        return cid


def load_chatops_config(path: str | Path | None = None) -> ChatopsConfig:
    p = Path(path or os.environ.get("SENTINEL_CHATOPS_CONFIG") or _DEFAULT_PATH)
    return ChatopsConfig.model_validate(yaml.safe_load(p.read_text(encoding="utf-8")))


def load_env_file(path: str | Path) -> list[str]:
    """读 KEY=VALUE 文件写进环境变量，返回读到的键名（不返回值，避免打进日志）。

    为什么要覆盖已有的值：S7 踩过坑——会话继承了旧的环境变量，连到了另一个应用。显式给了文件，就以文件为准。
    """
    keys = []
    for raw in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = raw.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip("'\"")
        keys.append(k.strip())
    return keys
