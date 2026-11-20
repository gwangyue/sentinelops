# M5 代码导览：飞书 ChatOps

> 按 `docs/design/templates/code-tour.md` 生成。M5 没有改调查图本身，只是在它外面加了一个"会推送的遥控器"。这份导览沿着 D21 在 EKS 上真实跑过的那一次走：**群里 @ 机器人 → 帮助卡片 → 点「巡检」→ 收到审批卡片 → 点「批准」→ 收到验证通过**。读完大约 30 分钟。
> 先记住一句话：**飞书只是 CLI 的另一个入口，不增加任何权力。规则还在原来的地方（白名单、方案哈希、执行器重新校验），飞书这一层只负责"翻译"和"拦截"。**

## 1. 路线图

```
飞书群（值班群）                        sentinel chatops serve（一个进程，EKS 上是 Deployment chatops）
                                       ┌───────────────────────────────────────────────────────────┐
 @机器人 /help ──长连接──▶ ① gateway._on_message ──▶ ② commands.parse ──▶ ③ CommandHandler.on_message
                                       │                                         │ 群 → 白名单 → commands → 参数
                                       │                                         ▼
 点「巡检 es-target」──卡片回调──▶ ④ route_card ──▶ on_button ──▶（同 ③）──▶ ⑤ _long_task：先回"进行中"
                                       │                                         │ 后台：patrol(remediate)
                                       │                                         ▼
                                       │                          调查图（core，没改）→ approval.requested 写审计
                                       │                                         │
 收到审批卡片 ◀──────────────── ⑥ Notifier.tick（按 seq 游标读审计表）◀─────────────┘
 点「批准」──卡片回调──▶ ④ route_card ──▶ ⑦ ApprovalHandler.on_card_action ──▶ decide_approval（和 CLI 共用）
                                       │                                         │ 后台 resume_online
                                       └─────────────────────────────────────────┼─────────────────┘
                                                                                 ▼
                                                     Postgres：审批记录 ──▶ ⑧ 执行器（另一个命名空间，不认识飞书）
                                                                                 │ 重新校验哈希 → 执行 → 验证
 收到"修复验证通过" ◀────── ⑥ Notifier（verify.passed）◀──────────────────────────┘
```

## 2. 逐站讲解

### ① gateway：把 SDK 对象拆成自己的结构

| 项 | 内容 |
|---|---|
| 位置 | `src/sentinel/chatops/gateway.py:48` `_on_message`；`:24` `_on_card`；`:75` `serve` |
| 人话 | 飞书通过长连接把消息和按钮点击推过来，这里把 lark SDK 的对象拆成 `ChatMessage` / `CardAction` 两个 dataclass，交给后面的纯逻辑，再把结果拼回 SDK 要的格式 |
| 输入 / 输出 | 输入：`P2ImMessageReceiveV1`、`P2CardActionTrigger`（SDK 类型）。输出：`ChatMessage`、`CardAction`（chatops 自己的 dataclass，**不是契约**，03 篇没有定义飞书的结构） |
| 关键概念 | **长连接**（WebSocket：进程主动连到飞书，飞书顺着这条连接把事件推回来。不需要公网入口，kind 和 EKS 用同一种方式） |
| 为什么这样设计 | 09 §5.2 最后一段：所有规则放在能用假数据测的纯逻辑里，gateway 只剩拆装，没有 if。消息处理放进线程：发卡片要走网络，不能占住长连接的事件循环 |
| 可能出错的地方 | 飞书根本没推事件。D21 踩过：prod 应用没开"获取群组中所有消息"，不 @ 的消息不会生成事件，表现是"机器人不回话"，日志里什么都没有。排查要看开放平台的事件日志（09 §9） |
| 面试可能被问 | 问：为什么用长连接，不用 HTTP 回调？答：HTTP 回调要有公网入口，会改变 01 篇"没有公网入口"的网络边界；长连接只需要出站。代价是鉴权在连接这一层，app_secret 就等于"能收到这个应用所有点击"的权力（T13） |

### ② parse：确定性解析，不调 LLM

| 项 | 内容 |
|---|---|
| 位置 | `src/sentinel/chatops/commands.py:66` `parse` |
| 人话 | 先去掉 `@_user_1` 这样的占位符，取第一个词当指令名，把 `--pack` 单独拿出来，剩下的是参数 |
| 输入 / 输出 | 字符串 → `Parsed(name, args, pack, rest)`；空消息（只 @ 了机器人）返回 `None` |
| 关键概念 | **@ 占位符**（飞书消息正文里不出现被 @ 的人名，而是 `@_user_1`、`@_user_2`，真实身份在另一个字段 `mentions` 里） |
| 为什么这样设计 | 09 §2 D5：只做固定指令。多一次 LLM 解析，就多一条"LLM 决定去注入故障"的路径，要重新论证评测和安全 |
| 可能出错的地方 | 认不出来的输入一律回帮助卡片，不猜意图。测试：`test_parse_strips_mention_placeholders_and_pack_option`、`test_unknown_input_returns_help_card_with_buttons` |
| 面试可能被问 | 问：用户体验会不会很差？答：用帮助卡片（模板 + 示例）和按钮来补。常用的无参数指令直接点按钮，不用记命令 |

### ③ CommandHandler.on_message：五道关 ★

| 项 | 内容 |
|---|---|
| 位置 | `src/sentinel/chatops/commands.py:128` |
| 人话 | 依次检查：是不是本环境的群 → 发指令的人在不在白名单 → 是不是 `/unfreeze`（一律拒绝）→ 指令在不在配置的 `commands` 里 → 参数逐个校验（target 在 `governance.yaml`，scenario 在专家包里）。全部通过才调用注入进来的函数 |
| 输入 / 输出 | `ChatMessage` → 通过注入的 `FeishuClient` 发卡片（没有返回值）。身份映射成 `human:<名字>`，和 CLI 的 `--as` 同一种格式 |
| 关键概念 | **依赖注入**（巡检、bench、冻结、发卡片都是构造时传进来的函数。线上接真实现，测试接记录调用的假函数，所以不访问网络也能测全部规则） |
| 为什么这样设计 | 09 §5.3。prod 不开放 bench，靠的是配置的 `commands` 里没有它，代码只有一份（D2）；`/freeze` 看 `high_risk_enabled`（D4）；`/unfreeze` 写死拒绝，不受任何配置影响 |
| 可能出错的地方 | 白名单外 → "已拒绝"卡片 + 日志；别的群 → 只记日志、不回话；参数错 → "参数不对"卡片。测试见 `tests/unit/test_chatops_commands.py`（24 个用例） |
| 面试可能被问 | 问：为什么别的群来的消息不回话，白名单外的人却回一张卡片？答：别的群说明配置串了（T14），回话等于把本环境的信息发到不该去的地方；白名单外的人就在本群，告诉他"没生效"，免得他以为执行了 |

### ④ route_card：按钮按 value 分流

| 项 | 内容 |
|---|---|
| 位置 | `src/sentinel/chatops/commands.py:347` `route_card`；`:162` `on_button`；按钮模板 `cards.py:150` `cmd_button` |
| 人话 | 卡片上的按钮有两种：帮助卡片上的指令按钮（value 是 `{"cmd": "/patrol es-target"}`），审批卡片上的「批准」「拒绝」（value 是 `{"approval_id", "decision"}`）。带 `cmd` 的交给指令，其余交给审批 |
| 输入 / 输出 | `CardAction` → `CardReply`（toast + 可选的新卡片） |
| 关键概念 | **卡片回调**（点按钮时飞书把 value 原样推回来。value 来自客户端，**不可信**） |
| 为什么这样设计 | 指令按钮只带一条指令文本，点了之后交给和手打同一个 `on_message` 重新做全部检查。所以按钮不会比手打多出任何权力，value 被伪造也一样。D21 发现的一个好处：按钮走卡片回调，不受"要不要 @"的影响 |
| 可能出错的地方 | 回调要在约 3 秒内返回，而且超时不重发（S7）。所以 `on_button` 只做最快的两个检查，指令放后台。测试：`test_command_button_goes_through_same_checks_as_typed_text` |
| 面试可能被问 | 问：有人伪造一个 `{"cmd": "/patrol prod-db"}` 的回调会怎样？答：和手打一样被参数校验拦下（不认识的目标），测试里就有这个用例 |

### ⑤ _long_task：先回"进行中"，跑完原地更新

| 项 | 内容 |
|---|---|
| 位置 | `src/sentinel/chatops/commands.py:310`；巡检结果 `:191` `_patrol_result` |
| 人话 | 巡检要几十秒（要诊断），所以先发一张"进行中"卡片，后台跑完再用 `patch_card` 把这张卡片改成结果。飞书的巡检用 `remediate` 模式：有问题就提议修复并停在审批 |
| 输入 / 输出 | 输入：注入的 `patrol(target, pack)`，线上就是 `core/graph/run.py: patrol`。输出：同一张卡片的更新 |
| 关键概念 | **uuid 幂等键**（发卡片时带一个 uuid，同一个 uuid 重发只出现一条消息。这里用消息 ID 生成，同一条消息被重复投递也只回一张卡片） |
| 为什么这样设计 | 09 §5.3 实现说明：审批卡片只有 remediate 模式才会出现，而修复照样停在审批，安全性不变。发"进行中"失败就不跑：用户看不到结果的任务不该开始 |
| 可能出错的地方 | 当前代码没有对应的冻结版本时，巡检会被拒绝（D20 踩过：版本哈希包含整个 `src/`），卡片显示"失败：……"。云上没有 mysql-target，点了也会失败（09 §9） |
| 面试可能被问 | 问：同一条消息被飞书重复投递会怎样？答：卡片按 uuid 去重只回一次，但指令会执行两次。这是记下的 `TODO(edge)`，要按 message_id 记已处理 |

### ⑥ Notifier.tick：推送读审计表，不在图的节点里调飞书 ★

| 项 | 内容 |
|---|---|
| 位置 | `src/sentinel/chatops/notifier.py:56` `tick`；事件类型 `:26` `NOTIFY_TYPES`；存储 `core/store/postgres.py:104` `audit_events_since` |
| 人话 | 每 5 秒按 `seq` 游标读一次审计表，把待审批、发现异常、验证结果这几类事件转成卡片发到群里，发成功了才推进游标 |
| 输入 / 输出 | `AuditEvent`（契约）→ 卡片 JSON。游标存在 `notify_cursor(env, last_seq)` 表里 |
| 关键概念 | **outbox 游标**（不让业务代码直接发通知，而是读"已经发生的事"的日志，记住读到哪了。至少投递一次，用审计事件 ID 当 uuid 去重） |
| 为什么这样设计 | 09 §1 第 3 条：图的代码一行都不改，Agent 行为不变，所以不用重新评测；飞书挂了不影响调查，恢复后从游标处补发。第一次启动从最新的 seq 开始，不把历史事件推出去 |
| 可能出错的地方 | D20 踩过：群 ID 没配，notifier 线程第一轮就异常退出，长连接照样在线，从外面看不出来。现在 `serve` 启动时先检查群 ID，缺了就拒绝启动 |
| 面试可能被问 | 问：为什么 `patrol.healthy` 不推？答：健康是常态，推了会刷屏，真正的告警反而被淹没。只在回复 `/patrol` 时告诉你 |

### ⑦ ApprovalHandler.on_card_action：和 CLI 同一个 decide_approval ★

| 项 | 内容 |
|---|---|
| 位置 | `src/sentinel/chatops/approvals.py:64`；共用逻辑 `src/sentinel/core/approval_view.py:91` `decide_approval` |
| 人话 | 点「批准」后：查群 → 查白名单 → 只从按钮里取 `approval_id` 和决定 → 调 `decide_approval`（和 CLI 是同一个函数，风险上限取配置）→ 立刻把卡片改成"已批准，执行中" → 后台恢复调查 |
| 输入 / 输出 | `CardAction` → `ApprovalDecision`（契约，审批人 `human:yuewang`）写进 Postgres；审计 `approval.decided` 的 reason 以 `[feishu]` 开头 |
| 关键概念 | **方案哈希**（`plan_hash`：方案内容的哈希。审批绑定的是这个哈希，执行器执行前重新算一遍对得上才执行）。这里哈希和方案都**从 Postgres 重新读**，不信卡片里的任何内容（T3、T4） |
| 为什么这样设计 | 09 §5.2。恢复放后台：飞书回调约 3 秒超时，`resume_online` 最多阻塞 300 秒。风险上限对「拒绝」也生效：高于上限的卡片本来就没有按钮，收到点击只可能是伪造的 |
| 可能出错的地方 | 重复点击 → "已被 xx 决定过"（`DuplicateError`）；高于上限 → 拒绝。测试见 `tests/unit/test_chatops_approval.py`；golden-demo 第 11 步离线回放这一段 |
| 面试可能被问 | 问：飞书的审批和 CLI 的审批，执行器分得出来吗？答：分不出来，也不需要分。执行器只认 Postgres 里的审批记录，渠道只记在 reason 前缀里，没有新增审计事件类型，契约没动 |

### ⑧ 部署：同一个镜像，多一个 Deployment

| 项 | 内容 |
|---|---|
| 位置 | `deploy/helm/sentinel/templates/chatops.yaml`；`deploy/aws/aws-up.sh` 的 preflight 和 workloads |
| 人话 | EKS 上在 `sentinel-system` 里多跑一个单副本的 `chatops`，命令是 `sentinel chatops serve`。飞书凭证由 `aws-up.sh` 从本机文件 `~/.config/sentinelops/feishu-prod.env` 建成 Secret，不经过 helm |
| 输入 / 输出 | 三个 Secret：`sentinel-agent-env`（Postgres、只读 ES）、`sentinel-llm`、`sentinel-feishu`。**不挂执行器的写凭证** |
| 关键概念 | **Recreate 策略**（更新时先停旧 Pod 再起新的。默认的滚动更新会短暂出现两个长连接，而长连接不广播，事件可能被旧 Pod 收走） |
| 为什么这样设计 | 09 §7。没有 Service、没有 Ingress：长连接只要出站。dev 和 prod 是两个飞书应用，凭证分开，app_secret 只放这一个命名空间 |
| 可能出错的地方 | 第一次启动时 Postgres 还没就绪，会崩溃重启两次再自己恢复（`TODO(edge)`：加 initContainer） |
| 面试可能被问 | 问：dev 的 app_secret 泄露了，最多能做什么？答：冒充 dev bot 在 dev 群发消息、收到 dev 应用的事件。审批还要过白名单，执行器还要重新校验，也碰不到 prod（09 §12） |

## 3. 先预测，再运行

| # | 要运行的命令 | 请先预测 | 实际结果（运行后填写） |
|---|---|---|---|
| 1 | `uv run pytest tests/unit/test_chatops_commands.py -k unfreeze -v` | 把开关 `high_risk_enabled` 打开，`/unfreeze` 还会被拒绝吗？测试里检查的是哪句话？ | |
| 2 | 在 `config/chatops.dev.yaml` 的 `commands` 里删掉 `bench`，再跑 `uv run pytest tests/unit/test_chatops_commands.py -k bench -v`（跑完改回来） | 哪些测试会失败、哪些不受影响？（提示：有的测试自己构造配置，有的读 yaml） | |
| 3 | 在 `src/sentinel/chatops/notifier.py` 顶部加一行 `import sentinel.exec`，跑 `uv run lint-imports`（跑完删掉） | 会破坏哪一条规则？错误信息里引用的是 09 篇哪一节？ | |

## 4. 自测题

<details><summary>题 1：卡片按钮里为什么只带 approval_id，不带方案哈希？</summary>

答案：卡片来自客户端，内容不可信（T3、T4）。服务端拿到 approval_id 之后，从 Postgres 重新读方案和哈希。就算有人伪造回调，最多只能对一个真实存在的审批做"批准/拒绝"，而且还要过白名单和风险上限。

</details>

<details><summary>题 2：飞书能 /freeze，却永远不能 /unfreeze，为什么？</summary>

答案：紧急停止要越方便越好，恢复要越谨慎越好（09 §12）。而且 /freeze 也不是默认开放的：和"飞书审批 high 风险"共用开关 `high_risk_enabled`，默认关闭，关闭时提示用 CLI。

</details>

<details><summary>题 3：M5 加了这么多代码，为什么不需要重新评测？</summary>

答案：推送读审计表、审批调用和 CLI 同一个函数、指令调用现有的函数，调查图、提示词、模型、工具、上下文和记忆策略都没改，Agent 的行为不变。但版本哈希包含整个 `src/`，所以要冻结一个"行为不变"的版本（v43）才能跑真实巡检，这是记账，不是评测。

</details>

<details><summary>题 4：D21 在 prod 群里发 /help 没反应，最后发现原因是什么？怎么排查的？</summary>

答案：prod 应用只开了"@当前机器人的消息"权限，没开"获取群组中所有消息"。不 @ 的消息，以及手打成纯文本的 @，飞书都不生成事件。排查顺序：本机凭证和 Pod 里的凭证哈希一致 → Pod 主动发消息成功（出站正常）→ 在 Pod 里起只打印事件的诊断客户端，也收不到（不是代码问题）→ 开放平台事件日志里 21:24 之后一条记录都没有（消息没变成事件）。

</details>

<details><summary>题 5：为什么 chatops 在 EKS 上用 Recreate，而不是默认的滚动更新？</summary>

答案：长连接不广播，同一个应用有多个客户端在线时，一条事件只投给其中一个（S7）。滚动更新时新旧 Pod 会同时在线一小段时间，点击可能被正在退出的旧 Pod 收走、处理到一半就被杀掉。单副本加 Recreate，同一时刻只有一个连接。

</details>
