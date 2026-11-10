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

- 里程碑：M2 进行中。场景扩充完成，shard_limit.with_distractor 的场景问题已修正；下一步是 D6 上下文层
- 最新版本：v12（v13 是 v11 提示词的对照组，按 06 §7.2 不上线；M1 发布版本是 v7）· 最新数据集标签：ds-20261110-5（es，17 个场景：dev 24 / heldout 10）
