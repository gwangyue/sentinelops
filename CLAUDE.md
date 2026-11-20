# CLAUDE.md

运维 Agent：确定性检测 → LLM 诊断 → 人审批 → 受控执行 → 验证 → 沉淀。围绕 ES，可扩展到 MySQL。
设计文档在 `docs/design/`。任务开始前，先读任务里指定的章节。

## 先读哪里

- 涉及数据结构：`03-contracts.md`（契约写死，需要修改时先停下来问，确认后补一条 ADR）
- 判断某件事在不在范围内：`00-overview.md` §5 不做清单
- 当前里程碑和验收命令：`08-plan.md`
- 端到端要做成什么样：`golden-demo.md`
- 想推翻某个架构决策：先读 `docs/design/adr/` 里对应的那条
- 里程碑结束时要写的报告：`docs/design/templates/`（验收报告和代码导览：保留专业术语，也要说人话）

## 规则

- 开始一个任务前，先说明思路和验证方式，再动手。步骤明确的任务列出计划；任务本身模糊的，先讲清楚打算怎么做、做完怎么判断对不对。
- 只实现主路径。边界情况写 `# TODO(edge): ...`，不要实现。
- 中文注释，解释"为什么"。每个模块的开头写清楚：负责什么、不负责什么、对应哪一节设计。
- 数据结构一律使用 `contracts/` 里的模型，不要另外定义相似的 dict。
- 新增依赖要在 PR 描述里说明理由。
- IMPORTANT：不要让 LLM 生成 `plan_hash`、审批 ID 或其他安全相关字段。

以下规则由工具强制执行，违反时检查会失败，修代码，不要绕过检查：
依赖方向（import-linter）· 契约修改（hook）· 写凭证只在 `exec/`（测试）· `interrupt()` 不能被宽泛的 except 包住（lint 规则）· `tests/` 不访问网络、不调用真实 LLM（测试夹具）· 评测必须指定数据集版本标签（运行器校验）· 被测 Agent 不开缓存（运行器校验）· 模型不能用别名（`version freeze` 校验）。详见 `08-plan.md` §6.4。

## 命令

```
uv sync
make check          # lint + 类型检查 + import 检查 + 测试
make demo-check     # golden-demo.md 中已打开的步骤
make compose-up     # 回放模式
make kind-up        # 本地全链路
```

## 完成标准

1. 任务的验收命令通过，`make check` 通过，并且贴出命令和输出作为证据。
2. 实现和设计不一致时，已经改了文档并写明原因。
3. 改动了 Agent 的行为（提示词、工具描述、上下文策略、记忆策略、模型）时，已经执行 `sentinel version freeze` 并填写 `note`。

## 当前状态

- 里程碑：**M5 飞书 ChatOps 进行中**（11-20 开始）。设计见 09-chatops.md、ADR-015，排期在 08 §3 M5。D17 已写完设计；S7 已通过（spike-results.md S7：回调约 3 秒超时、不重发；长连接不广播；用国内飞书 open.feishu.cn）。D18 推送已完成（`sentinel chatops notify`，dev 群实测）。下一步 D19 卡片审批。M5 不改 Agent 行为，不需要冻结新版本，不需要重新评测
- 上一个里程碑：**M4 完成**（tag `m4`，报告 docs/reports/M4-report.md、代码导览 M4-code-tour.md）。08 的四个里程碑全部结束。M4：D13 通用化（tag `m4-base`）+ MySQL 专家包；D14 周报、MCP；D15–D16 EKS 悉尼（组织 SCP 只放开 ap-southeast-2，账号是免费套餐、节点 m7i-flex.large；`make aws-up / aws-patrol / aws-down`）。零改动验收有 5 条例外（见 M4 报告 §3）。缺口：漂移检查（M2–M4 都没做）。已决定（11-20）：v42 不评测，上线版本仍是 v41；AWS 保持免费套餐。待决定：控制台访问条目（M4 报告 §9）
- 最新版本：v41（L3 开，L4、L5 不上线；es + mysql 两个专家包）· 数据集标签：es `ds-20261111-1`（17 个场景 34 条）、mysql `ds-20261111-2`（3 个场景 6 条）· 事故流：datasets/streams/es.yaml
