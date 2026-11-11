# ADR-014 审计事件增加 freeze.enabled / freeze.disabled

- 状态：已接受（2026-11-11，人确认）
- 相关：03 篇 §5.1，07 篇 §7，ADR-011（同类的新增）

## 背景

07 §7 要求紧急停止"冻结和解冻都写入审计"，但 03 §5.1 的事件类型表里没有对应的类型。冻结时作废的待审批可以用已有的 `approval.expired`，冻结和解冻本身没有合适的类型。

## 决策

- 03 §5.1 的事件类型表增加 `freeze.enabled` 和 `freeze.disabled`。payload 里只有 `reason`；`actor` 是操作人（`human:<名字>`）。
- 冻结时作废的待审批：写一个 `rejected` 的审批决定（审批人 `system:freeze`），审计记 `approval.expired`，payload 里带 `reason: frozen`。
- 冻结状态本身存在 Postgres 的 `freeze_events` 表，只追加，最新一行决定当前状态。执行器每次执行前都读，不依赖 agent-api（07 §7）。

## 放弃的方案

| 方案 | 放弃原因 |
|---|---|
| 只记在冻结状态表里，不进审计链 | 和 07 §7 不一致；`audit verify` 也就保护不到冻结记录 |
| 复用 `investigation.escalated` | 冻结不属于某一次调查，`investigation_id` 为空，语义也不对 |

## 后果

- `AuditEvent.type` 本来就是字符串，契约代码不用改，只改 03 的表。旧数据不受影响。
