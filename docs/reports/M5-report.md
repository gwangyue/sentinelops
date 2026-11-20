# M5 验收报告：飞书 ChatOps

> 按 `docs/design/templates/milestone-report.md` 生成，2026-11-20。读者是负责人，20 分钟读完。
> 时间：飞书控制台和本机日志是本地时间（UTC+9）；云上审计和 Pod 日志是 UTC。下文不加说明的是 UTC。

## 0. 一句话总结

**飞书已经能当 CLI 的第二个入口用了：在群里能收到告警和审批卡片，能点按钮批准，能发几条固定指令。kind（dev bot）和 EKS（prod bot）上都从"发现问题"一路跑到了"验证通过"。安全规则一条没放松，执行器根本不知道审批来自飞书。** 可以收尾 M5。有两件事需要你决定（§9）。

| 状态 | 数量 |
|---|---|
| 完成并有证据 | 08 篇 M5 表的 5 项（D17–D21）；golden-demo 第 1–11 步；kind 和 EKS 各跑通一次全链路；独立审阅（1 条已修，3 条已处理） |
| 部分完成 | 0 |
| 未完成 | 模型漂移检查（G2，08 §6.6 第 3 步）：和 M2–M4 一样没做。M5 没改 Agent 行为，影响有限 |

## 1. 任务完成情况

5 天的任务都做了，每一项都有实测或测试作证据。

| 任务（08 篇） | 人话说明：做了什么 | 证据 | 状态 |
|---|---|---|---|
| D17 设计 + S7 技术验证 | 09 篇、ADR-015；实测飞书长连接：回调约 3 秒超时、不重发；长连接不广播 | `spike-results.md` S7；提交 53128b9、d0674f3 | 完成 |
| D18 推送 | notifier 按审计表的 seq 游标把 7 类事件推成卡片；游标存 `notify_cursor` 表 | dev 群实测；`test_chatops_notifier.py` 9 个测试；提交 a00ad89 | 完成 |
| D19 卡片审批 | 点「批准」「拒绝」走白名单 → 风险上限 → `decide_approval`（和 CLI 共用）→ 后台恢复调查 | `test_chatops_approval.py` 12 个测试；golden-demo 第 11 步；提交 27ee2d1 | 完成 |
| D20 固定指令 + kind 全链路 | `/help /patrol /inv /approvals /bench /freeze`，`/unfreeze` 一律拒绝；帮助卡片带按钮；dev bot 在 kind 上跑通，这也是 D19 卡片审批的第一次真实点击 | `test_chatops_commands.py` 24 个用例；kind 审计 seq 153–161（§2）；提交 b4ffd68、17fcf34 | 完成 |
| D21 EKS + 收尾 | Helm 加 `chatops` Deployment；`aws-up.sh` 从本机文件建 `sentinel-feishu`；prod bot 在 EKS 上跑通；独立审阅；本报告和代码导览 | 云上审计 seq 4–11（§2）；提交 5240166 和本次收尾提交 | 完成 |

## 2. 验收命令

08 篇 M5 的 4 条验收命令都通过了。

| 命令 | 预期 | 实际（关键输出） | 是否通过 |
|---|---|---|---|
| `make check` | lint、类型、import-linter、测试全过 | `Contracts: 8 kept, 0 broken.`；`296 passed` | ✓ |
| `make demo-check` | 第 1–11 步都打开、都通过 | `11. 在飞书里审批 [已打开] 通过`；`已打开的步骤中未通过：0`（`tools/demo_steps.yaml` 改成 `opened_through: M5`） | ✓ |
| `sentinel chatops serve`（dev bot，kind） | `/help` → `/bench inject` → 审批卡片 → 点批准 → verify.passed；开关关闭时 `/freeze` 被拒 | 见下表①；`/freeze 测试` 回了"已拒绝：本环境没有开启 high_risk_enabled"，审计里没有 `freeze.*` | ✓ |
| `make aws-up && （prod bot：/patrol + 一次审批） && make aws-down` | 云上走完一次审批 | 见下表②；aws-down 见 §8 | ✓ |

① kind（本机 Postgres，版本 v43）：

| seq | 事件 | actor | 内容 |
|---|---|---|---|
| 153 | investigation.started | agent | mode=remediate，飞书的「巡检」按钮触发 |
| 157 | plan.proposed | agent | `es.raise_watermark_temporarily`，high_pct=94 |
| 159 | approval.decided | **human:yuewang** | reason **`[feishu]`**，approved |
| 160 | action.executed | executor | succeeded，哈希一致 |
| 161 | verify.passed | agent | `status=green unassigned=0` |

② EKS 悉尼（云上 Postgres，版本 v1 = 提交 5240166）：

| seq | 时间 | 事件 | actor | 内容 |
|---|---|---|---|---|
| 4 | 13:09:04 | investigation.started | agent | mode=remediate |
| 7 | 13:09:15 | plan.proposed | agent | `es.enable_allocation`，预演：5 个未分配分片 |
| 8 | 13:09:15 | approval.requested | agent | plan_hash `sha256:f59db5ef…` |
| 9 | 13:09:30 | approval.decided | **human:yuewang** | reason **`[feishu]`**，approved |
| 10 | 13:09:31 | action.executed | executor | succeeded，哈希一致 |
| 11 | 13:09:41 | verify.passed | agent | `unassigned_shards=0 status=green` |

## 3. 和设计不一致的地方

一共 6 处，文档都已经同步改了。

| 位置 | 设计里写的 | 实际做的 | 原因 | 文档 |
|---|---|---|---|---|
| ADR-015 决策 4、08 §3 M5 | 不冻结新版本 | 冻结了 v43（行为不变，不评测，上线版本仍是 v41） | 版本哈希包含整个 `src/`。chatops 的代码一改，当前代码就没有对应版本，真实巡检会被拒绝（D20 实测）。v38–v41 有同样的先例 | ✓ ADR-015、08、09 §9（独立审阅发现 2） |
| 09 §5.3 `/patrol` | 对应 `patrol`，模式没写 | 飞书的巡检用 `remediate` 模式 | 只有 remediate 才会出审批卡片；修复照样要先审批 | ✓ 09 §5.3 |
| 09 §5.3 target 校验 | "target 必须在 governance.yaml 里" | 用 `governance.yaml` 的 `mcp.targets`（目标 → 专家包） | 目前只有这里登记了这张表 | ✓ 09 §5.3 |
| 09 §7 prod 凭证 | preflight 检查环境变量 | 读本机文件 `~/.config/sentinelops/feishu-prod.env`，用 `kubectl --from-env-file` 建 Secret | 和 dev 的 `--env-file` 一致，避开 S7 继承旧环境变量的坑 | ✓ 09 §7 |
| 09 §6 prod 群 | 未写明 | prod 群的指令要真的 @ 机器人 | prod 应用只开了"@机器人的消息"权限，不 @ 的消息飞书不生成事件（D21 实测，见 §8） | ✓ 09 §9 |
| 范围之外 | — | `make kind-remount`（提交 bddf80c） | 顺手修的运维问题：Docker 重启后 es-target 起不来（独立审阅发现 4） | ✓ CLAUDE.md |

## 4. 简化和跳过的地方

都是边界情况，不影响主路径。

| 位置 | 内容 | 人话说明 | 影响 |
|---|---|---|---|
| `chatops/gateway.py`（`_on_message`） | TODO(edge)：消息重复投递时指令执行两次 | 卡片按 uuid 去重只回一次，但巡检可能跑两次 | 低：巡检是只读的；有同一集群修复互斥兜底 |
| `docs/design/09-chatops.md` §9 | TODO(edge)：云上帮助卡片出现「巡检 mysql-target」 | chart 没部署 MySQL，点了会因为缺凭证而失败（D21 实测过一次） | 低：只是一个会失败的按钮 |
| `deploy/helm/.../chatops.yaml` | TODO(edge)：没有等 Postgres 的 initContainer | 第一次启动会崩溃重启 2 次，之后自己恢复 | 低 |
| `chatops/approvals.py:_resume` | TODO(edge)：恢复失败时卡片停在"执行中" | 只有日志 | 中：要看日志才知道 |
| `chatops/commands.py:_long_task` | TODO(edge)：更新卡片失败不补发 | 只有日志 | 低 |
| `chatops/notifier.py:tick` | TODO(edge)：积压大量事件时不合并 | 长时间停机后会逐条推送 | 低 |
| `cli/main.py:_bench_subprocess` | TODO(edge)：`bench record` 只处理了 es；子进程的 port-forward 不会随进程退出 | 只在 dev 用 | 低 |

## 5. 关键数字

数字都是单次实测，只说明"能跑通、够快"，不说明 Agent 好不好（M5 没改 Agent，也没评测）。

| 指标 | 数值 | 含义 | 怎么读 |
|---|---|---|---|
| 从点「批准」到验证通过 | kind 2 秒，EKS 11 秒 | 单次 | 飞书这一层没有引入明显的延迟；主要时间花在执行器轮询和验证上 |
| 卡片回调里的处理 | 毫秒级（一次 Postgres 写入） | 估算 | 远小于飞书约 3 秒的回调时限（S7），恢复调查放在后台 |
| `/patrol` 的诊断 | 约 10 秒，输入约 1 万 token | 单次（EKS seq 4→6） | 和 M4 的诊断一样，飞书没有多加 LLM 调用 |
| EKS 费用 | 约 1.3 小时 × $0.95/小时 ≈ $1.2 | 估算（S6 的单价） | 比计划多用了约 30 分钟，花在排查"机器人不回话"上 |
| 测试 | 296 个（chatops 45 个用例） | 计数 | 全部离线，不访问网络 |

## 6. 护栏是否真的生效

和 M5 有关的护栏都故意违反了一次，全部被拦住。

| 护栏 | 故意违反的方式 | 结果 | 生效？ |
|---|---|---|---|
| chatops 不能 import 执行器 | 在 `chatops/commands.py` 末尾加 `import sentinel.exec`，跑 `lint-imports` | `chatops 不 import 执行器 … BROKEN`；`Contracts: 7 kept, 1 broken.`（已还原） | ✓ |
| 下层不能依赖 chatops | 在 `core/approval_view.py` 加 `import sentinel.chatops.cards` | `core / governance / exec / eval 不依赖 chatops … BROKEN`（已还原） | ✓ |
| 白名单 | D21：prod 审批人还没配时，在值班群 @ 机器人 | 日志 `白名单外的人发指令：open_id=ou_7d92…`，指令没执行（真实环境） | ✓ |
| `/freeze` 开关 | dev 群发 `/freeze 测试`（开关关闭） | 回"已拒绝"，审计里没有 `freeze.*`，`frozen False`（真实环境） | ✓ |
| prod 不开放 bench、`/unfreeze` 一律拒绝 | 单元测试：prod 配置下发 bench；开关打开时发 `/unfreeze` | `test_bench_is_refused_on_prod_config`、`test_unfreeze_always_refused_even_with_switch_on` 通过 | ✓ |
| 未提交的改动不能跑调查 | D20：有未提交的 chatops 改动时点「巡检」 | 卡片显示"失败：影响行为的文件有未提交的改动……"（真实环境） | ✓ |
| 契约不变 | `git diff m4..HEAD -- src/sentinel/contracts` | 0 行 | ✓ |
| 凭证不进仓库、不进镜像 | 在仓库里建 `deploy/feishu-prod.env`，跑 `git check-ignore -v` | `.gitignore:7:*.env`；`.dockerignore` 同样加了 `*.env`（提交 2b6cb26） | ✓ |

## 7. 代码规模和需要人深读的模块

M5 新增约 1100 行代码，其中 3 个模块需要你亲自读懂。

| 模块 | 行数 | 测试数 | 需要深读？ | 原因 |
|---|---|---|---|---|
| `chatops/commands.py` | 351 | 24 | ★ | 五道关：群、白名单、`/unfreeze`、指令白名单、参数校验。飞书能触发什么，全在这里 |
| `chatops/approvals.py` + `core/approval_view.py` | 113 + 126 | 12 | ★ | 审批只信 approval_id，风险上限在服务端再校验一次，和 CLI 共用 `decide_approval` |
| `chatops/notifier.py` | 84 | 9 | ★ | 推送读审计表、游标、至少投递一次。独立审阅发现 1 就在这里 |
| `chatops/cards.py` | 181 | （随上面测试） | — | 卡片模板，纯函数 |
| `chatops/gateway.py` | 93 | — | — | SDK 拆装，没有规则 |
| `chatops/config.py`、`client.py`、`fake.py` | 62 + 62 + 27 | — | — | 配置、真实/假客户端 |
| `deploy/helm/.../chatops.yaml`、`aws-up.sh` 飞书部分 | 47 + 13 | — | — | 部署 |

## 8. 风险和下一步

最担心的是**飞书这一层"静默失效"**：进程看起来在线，其实什么都收不到、或者什么都推不出去。M5 碰到了三次：

1. D20：群 ID 没配，notifier 线程启动就退出，长连接照常在线。已修：启动时检查。
2. D21：prod 群不 @ 就收不到消息，飞书根本不生成事件，我们的日志里什么都没有。排查用了约 30 分钟，最后靠开放平台的事件日志定位。已写进 09 §9。
3. 独立审阅发现 1：Postgres 抖一下，notifier 就永久停推。已修：`run_forever` 每轮兜住异常，加了测试 `test_run_forever_survives_a_failed_round_and_catches_up`。

还剩的缺口：chatops 没有存活探针。线程被杀掉、长连接断了重连失败，现在都发现不了。如果以后长期在线运行，建议加一个"最近一次成功推送 / 心跳"的健康检查。

aws-down 已完成：`Destroy complete! Resources: 60 destroyed.`；残留检查 `EKS 集群 0，实例 0，NAT 0，EIP 0，VPC 端点 0，EBS 卷 0`，只保留了 state 桶。

**负责人要做的**（08 §6.6 第 6 步，约 1.5 小时）：读本报告；跟着 `M5-code-tour.md` 走一遍，做"先预测再运行"；亲手挑 2 条护栏故意违反一次。

## 9. 需要你决定的事

1. **prod 群要不要开"获取群组中所有消息"权限。** **已决定（11-20）：不开**，值班群的指令要 @ 机器人。
   - 不开（现状）：值班群里的指令要 @ 机器人；按钮不受影响。能防误触，权限最小。
   - 开：和 dev 一样，不 @ 也能用。代价是值班群的每条消息都推给 bot，不是指令的消息会收到一张帮助卡片。
   - **建议不开。**
2. **本次收尾修了 `notifier.py`，当前代码又没有对应的冻结版本了。**
   - 现在冻结 v44（行为不变，只为代码哈希记账）。
   - 等下次要跑真实巡检时再冻结。
   - **建议等下次用时再冻结**，免得版本表里堆一串只为记账的版本。
3. **模型漂移检查（G2）**：M2–M5 都没做。M5 没改 Agent 行为，可以继续往后放，但它是 08 §6.6 的固定动作，M4 报告里也记过一次。建议在下一个会改 Agent 行为的里程碑开始前补做。

## 术语表

| 术语 | 白话解释 |
|---|---|
| ChatOps | 在聊天工具里看告警、做审批、发指令，不用登录服务器敲命令 |
| 长连接 | 进程主动连到飞书并保持连接，飞书顺着这条连接把事件推回来；不需要公网入口 |
| 卡片回调 | 点卡片按钮时，飞书把按钮里预先放好的 value 原样推回来；内容来自客户端，不可信 |
| open_id | 飞书给每个用户的 ID，按应用区分：同一个人在 dev 应用和 prod 应用里的 open_id 不一样 |
| outbox 游标 | 推送不由业务代码直接发，而是读"已经发生的事"的日志，记住读到第几条 |
| uuid 幂等键 | 发消息时带一个唯一编号，同一个编号重发只出现一条消息 |
| 方案哈希（plan_hash） | 修复方案内容的哈希；审批绑定它，执行器执行前重算一遍对得上才执行 |
| Recreate | 部署更新时先停旧 Pod 再起新的，保证同一时刻只有一个长连接 |
| 冻结版本 | 把当前的提示词、模型、工具、策略和代码的哈希登记成一个版本号；没有版本号的调查不能写结果 |
| 漂移检查（G2） | 用同一个数据集重跑上一个上线版本，看模型服务商那边有没有悄悄变化 |
