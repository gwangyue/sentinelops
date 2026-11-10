# ADR-011 专家包声明根因标签；审计事件增加 patrol.healthy

- 状态：已接受（2026-11-10）
- 相关：03 篇 §3.1、§4.1、§5.1；golden-demo 第 1 步和第 3 步；06 篇 §3.1

## 背景

M1 D3 实现调查图时碰到两处契约缺口：

1. `Hypothesis.root_cause_label` 要和场景的真实答案比对（top-1 正确率的计算依据），但契约里没有规定合法标签从哪里来。如果让模型自由填写，`es.disk_watermark_high` 和 `es.disk_high_watermark` 会被当成两种根因。
2. golden-demo 第 1 步要求巡检健康时写审计事件 `patrol.healthy`，但 03 §5.1 的事件类型表里没有这一项。

## 决策

- `PackManifest` 增加可选字段 `root_causes: list[RootCauseDecl]`，每项包含 `label` 和 `description`，默认为空列表。诊断提示词会列出这些标签；模型给出表外的标签时，由代码改为 None。
- 03 §5.1 的事件类型表增加 `patrol.healthy`。

## 放弃的方案

| 方案 | 放弃原因 |
|---|---|
| 每个专家包自带一个约定好的 `root_causes.yaml` | 约定在契约之外，校验不了 |
| 健康时写 `investigation.closed` 加 `outcome=healthy` | golden-demo 要单独检查"巡检健康"这件事，独立的事件类型更直接 |

## 后果

- 两处都只是**新增**，字段带默认值，D1 冻结的 v1 样例数据照样能读，契约测试已经验证。
- 改根因表会改变诊断提示词，所以根因表的哈希也计入 AgentVersion（`prompts` 里的 `root_causes:<pack>` 一项）。
