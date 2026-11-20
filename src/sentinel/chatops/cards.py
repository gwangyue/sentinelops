"""飞书卡片模板（09 §4.1、§4.3）：纯函数，输入契约模型和审计事件，输出卡片 JSON（消息卡片 1.0 结构）。

负责：每种推送长什么样；审批卡片要不要放按钮。
不负责：发送（client.py）；按钮被点之后做什么（approvals.py）。

两条规则：
1. 审批卡片的正文就是 ApprovalView.lines()，和 `sentinel approvals show` 是同一份（09 §4.3）。
2. 按钮的 value 里只带 approval_id 和决定：服务端收到之后从 Postgres 重新读方案和哈希，不信任卡片内容（T3、T4）。
"""

from typing import Any

from sentinel.chatops.config import ChatopsConfig
from sentinel.contracts import AuditEvent
from sentinel.core.approval_view import ApprovalView, risk_allowed
from sentinel.core.store import InvestigationRecord

Card = dict[str, Any]


def _card(cfg: ChatopsConfig, title: str, template: str, elements: list[dict[str, Any]]) -> Card:
    # update_multi：群里所有人看到同一张卡片的更新（D19 点击之后要原地改成"已批准"）
    return {
        "config": {"wide_screen_mode": True, "update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": f"[{cfg.env_label}] {title}"}, "template": template},
        "elements": elements,
    }


def _md(text: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": text}


def _cli(cmd: str) -> dict[str, Any]:
    return _md(f"CLI：`{cmd}`")


def approval_card(cfg: ChatopsConfig, v: ApprovalView, note: str | None = None) -> Card:
    """note：点击之后写在卡片底部的一句话（"已批准，执行中"之类）。只有 pending 的审批才有按钮。"""
    body = "\n".join(f"**{k}**：{val}" for k, val in v.lines())
    head = f"调查 `{v.plan.investigation_id}` · 集群 `{v.plan.target}` · 审批 `{v.request.id}` · 状态 {v.state}"
    elements: list[dict[str, Any]] = [_md(head), {"tag": "hr"}, _md(body), {"tag": "hr"}]
    if v.decision is not None:
        # 已经决定过：卡片原地更新成结果，按钮去掉，免得有人再点
        d = v.decision
        word = "已批准" if d.decision == "approved" else "已拒绝"
        elements.append(_md(f"**{word}**（{d.approver}，{d.decided_at:%m-%d %H:%M:%S %Z}）{note or ''}"))
        template = "green" if d.decision == "approved" else "grey"
        return _card(cfg, f"{word}：{v.plan.action_id}", template, elements)
    if risk_allowed(v.spec.risk, cfg.max_risk):
        elements.append(
            {
                "tag": "action",
                "actions": [
                    _button("批准", "primary", v.request.id, "approved"),
                    _button("拒绝", "danger", v.request.id, "rejected"),
                ],
            }
        )
    else:
        # 09 §2 D3：高于上限的风险照样推送、完整显示，但不给按钮，提示到 CLI 审批
        elements.append(_md(f"⚠️ **{v.spec.risk} 风险动作，飞书审批未开启**（上限 {cfg.max_risk}），请到 CLI 审批："))
    elements.append(_cli(f"sentinel approvals approve {v.request.id} --as <你的名字>"))
    return _card(cfg, f"待审批：{v.plan.action_id}", "orange", elements)


def _button(text: str, kind: str, approval_id: str, decision: str) -> dict[str, Any]:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": text},
        "type": kind,
        "value": {"approval_id": approval_id, "decision": decision},
    }


def anomaly_card(cfg: ChatopsConfig, e: AuditEvent, rec: InvestigationRecord | None) -> Card:
    """diagnosis.completed：只放摘要（集群、检测器、H1），不贴原始观测（09 §8 T15）。"""
    hyps = e.payload.get("hypotheses") or []
    h1 = hyps[0] if hyps else None
    target = rec.target if rec else "?"
    detectors = sorted({f.get("detector", "?") for f in rec.findings}) if rec else []
    lines = [
        f"**集群**：`{target}`",
        f"**检测器**：{', '.join(detectors) or '—'}",
        f"**H1**：{h1['label']}（置信度 {h1['confidence']}）" if h1 else "**H1**：没有假设",
        f"**证据支持率**：{e.payload.get('evidence_support_rate', '—')}",
    ]
    return _card(
        cfg,
        f"发现异常：{target}",
        "red",
        [_md("\n".join(lines)), _cli(f"sentinel investigations show {e.investigation_id}")],
    )


def event_card(cfg: ChatopsConfig, e: AuditEvent) -> Card | None:
    """除了审批和异常之外的推送。返回 None 表示这个类型不推送。"""
    inv = e.investigation_id
    p = e.payload
    if e.type == "investigation.escalated":
        return _card(
            cfg,
            "需要人接手",
            "red",
            [_md(f"调查 `{inv}`\n**原因**：{p.get('reason')}"), _cli(f"sentinel investigations show {inv}")],
        )
    if e.type == "verify.passed":
        return _card(
            cfg,
            "修复验证通过",
            "green",
            [
                _md(f"调查 `{inv}` · 方案 `{p.get('plan_id')}`\n{p.get('detail', '')}"),
                _md("确认根因之后才会写入记忆（只能在 CLI 做）："),
                _cli(f"sentinel investigations confirm {inv} --root-cause H1 --as <你的名字>"),
            ],
        )
    if e.type == "verify.failed":
        return _card(
            cfg, "验证失败，正在回滚", "red", [_md(f"调查 `{inv}` · 方案 `{p.get('plan_id')}`\n{p.get('detail', '')}")]
        )
    if e.type == "action.rolled_back":
        return _card(cfg, "已回滚", "red", [_md(f"调查 `{inv}` · 方案 `{p.get('plan_id')}` · 状态 {p.get('status')}")])
    if e.type == "freeze.enabled":
        return _card(
            cfg,
            "系统已冻结",
            "red",
            [
                _md(f"**操作人**：{e.actor}\n**原因**：{p.get('reason')}\n执行器拒绝所有动作，待审批全部作废"),
                _cli("sentinel unfreeze --reason ... --as <你的名字>"),
            ],
        )
    return None


# ---- 固定指令（09 §5.3；D20）----

# 每条指令的模板和示例。帮助卡片只列出本环境 commands 里开着的那些（prod 没有 bench）
COMMAND_HELP: dict[str, tuple[str, str]] = {
    "help": ("/help", "列出可用指令"),
    "patrol": ("/patrol <target> [--pack es]", "/patrol es-target"),
    "inv": ("/inv list · /inv show <调查ID>", "/inv show inv_01..."),
    "approvals": ("/approvals list", "每条待审批重新发一张审批卡片"),
    "bench": ("/bench inject|revert|record <scenario> [--pack es]", "/bench inject es.disk_watermark.single"),
    "freeze": ("/freeze <原因>", "/freeze 误操作，先停下"),
}


def cmd_button(text: str, cmd: str, kind: str = "default") -> dict[str, Any]:
    """指令按钮：value 只带一条指令文本。点击之后和手打这条指令走同一套解析和校验（commands.py），
    所以按钮不会比手打多出任何权力；value 被伪造也一样。"""
    return {"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": kind, "value": {"cmd": cmd}}


def help_card(cfg: ChatopsConfig, targets: list[str], note: str | None = None) -> Card:
    """09 §2 D5：模板 + 示例；常用的无参数指令做成按钮（每个目标一个「巡检」、「待审批列表」）。"""
    rows = [
        f"`{tpl}`\n　例：{ex}" for name, (tpl, ex) in COMMAND_HELP.items() if name == "help" or name in cfg.commands
    ]
    elements: list[dict[str, Any]] = []
    if note:
        elements.append(_md(note))
    elements.append(_md("\n".join(rows)))
    elements.append(_md("解冻和确认根因只能在 CLI 做：`sentinel unfreeze ...` · `sentinel investigations confirm ...`"))
    buttons = [cmd_button(f"巡检 {t}", f"/patrol {t}", "primary") for t in targets if "patrol" in cfg.commands]
    if "approvals" in cfg.commands:
        buttons.append(cmd_button("待审批列表", "/approvals list"))
    if buttons:
        elements.append({"tag": "action", "actions": buttons})
    return _card(cfg, "指令帮助", "blue", elements)


def text_card(
    cfg: ChatopsConfig, title: str, body: str, template: str = "blue", buttons: list[dict[str, Any]] | None = None
) -> Card:
    """指令的回复：已开始 / 结果 / 拒绝，都是一个标题加一段 markdown。"""
    elements: list[dict[str, Any]] = [_md(body)]
    if buttons:
        elements.append({"tag": "action", "actions": buttons})
    return _card(cfg, title, template, elements)
