# M4 扩展与上云 验收报告

> 按 `docs/design/templates/milestone-report.md` 生成。范围：`m3..m4`（D13 通用化 + MySQL 专家包；D14 治理周报 + MCP 只读服务；M2/M3 审阅修复和 M4 例外修复；S6 和 D15–D16 上云），19 个提交加收尾提交。
> 上线版本 **v41**（v42 已冻结、未评测，见 §9），数据集标签 es **ds-20261111-1**、mysql **ds-20261111-2**。读者：项目负责人，目标 20 分钟读完。

## 0. 一句话总结

**M4 做成了：加一个新领域（MySQL）只需要写一个专家包，core 和执行器不用改；治理周报的每个数字都能用 SQL 从审计表里重算；外部 Agent 能通过 MCP 用只读工具；整套系统能用一条命令部署到 AWS EKS，在云上跑通一次调查，再用一条命令拆干净。上云时碰到账号限制（只能用悉尼、免费套餐）和 3 个代码问题，都已处理。M4 可以结束；漂移检查按你的决定记为缺口，v42 还没评测。**

| 状态 | 数量 |
|---|---|
| 完成并有证据 | 08 篇 M4 表的 4 项任务（D13、D14 上午下午、D15–D16）；golden-demo 第 1–10 步；独立审阅（1 条已修、1 条记录） |
| 部分完成 | 2：零改动验收有 5 条记录在案的例外（§3）；执行器的网络策略只在 kind 上实测，EKS 已销毁未测（§6） |
| 未完成（按你的决定） | 2：模型漂移检查；v42 评测 |

## 1. 任务完成情况

人话结论：四项任务都做完了；MySQL 在 kind 上跑通了完整的修复链路，上云在 EKS 上跑通了一次调查。

| 任务（08 篇） | 人话说明：做了什么 | 证据 | 状态 |
|---|---|---|---|
| D13 MySQL 专家包，core/exec 零改动 | 先做一次"通用化"提交（tag `m4-base`）：core/exec 里 4 处写死 ES 的地方改成按专家包分派。之后加 MySQL：5 个只读工具、3 个检测器、1 个动作、3 个场景 | `git diff --stat m4-base..<D13 结束> -- src/sentinel/core src/sentinel/exec` 为空；mysql dev/heldout top1 1.00；修复模式不安全提议 0，connections_exhausted 6/6 resolved；kind 上完整 UC2（spike-results D13） | 完成 |
| D14 周报 | `sentinel report weekly`：5 个数 + "需要你决定的事"，每个数都是一条对审计表的 SQL，Postgres 和 sqlite 上同一份 SQL | 第一次在真实审计上运行（2026-W46）：调查 26 次、修复 3 次 3/3 成功……（spike-results D14）；golden-demo 第 10 步通过 | 完成 |
| D14 MCP 只读服务（可选） | `sentinel mcp serve --as <调用方>`：只导出 `mcp_export: true` 的只读工具，按调用方限制目标，只返回摘要，每次调用写审计 | 官方 MCP 客户端实测：15 个工具；白名单外的目标被拒；调用进审计 | 完成 |
| D15–D16 上云 | 镜像、Helm chart、Terraform（VPC、EKS、ECR、EBS CSI、Pod Identity）；`make aws-up / aws-patrol / aws-down`；部署到悉尼，跑一次调查，销毁 | 见 §2 | 完成 |

## 2. 验收命令

人话结论：08 篇 M4 的验收命令都跑过了（D15–D16 按 11-20 的决定改为"跑通一次调查"）。

| 命令 | 预期 | 实际（关键输出） | 是否通过 |
|---|---|---|---|
| `git diff --stat m4-base..HEAD -- src/sentinel/core src/sentinel/exec` | 为空 | 不为空：5 条例外（§3），其余只有注释（独立审阅逐文件比对过语法树） | 有例外 |
| `sentinel eval run --pack mysql --split dev --version v39 --reps 3` | 能跑 | top1 1.00（D13，6 次运行） | ✓ |
| `sentinel report weekly` | 5 个数 | 见 §1；golden-demo 第 10 步检查"每个数都能用 SQL 重算"通过 | ✓ |
| `make aws-up` | 环境建好 | Terraform 60 个资源；es-target、es-memory green；agent-api、执行器就绪；种子数据 2000 条 | ✓（第 3 次才成功，§3 第 1、2 行） |
| `make aws-patrol` | 在 EKS 上走完一次调查 | 健康巡检 `0 个问题，用时 1.5 秒`；注入后 `inv_01M7VDY6ZGY15264FCTV9JPP85 状态 diagnosed 问题 2 个 用时 12.0 秒`，证据支持率 100% | ✓ |
| `make aws-down` | 销毁，无残留 | `Destroy complete! Resources: 60 destroyed.`；按类型直接查：集群、实例、NAT、EIP、端点、卷全部 0 | ✓ |
| `make check` / `make demo-check` | 全绿 | 251 passed；golden-demo 打开到 M4，第 1–10 步通过 | ✓ |

## 3. 和设计不一致的地方

人话结论：有 9 处和原设计不同，都写进了文档；没有改契约。

| 位置 | 设计里写的 | 实际做的 | 原因 | 文档是否已同步 |
|---|---|---|---|---|
| 01 §5.2、ADR-007 | 东京区域 | **悉尼**（ap-southeast-2） | 账号所在组织的 SCP 只放开悉尼（实测 17 个区域） | ✓ ADR-007 修订 |
| 01 §5.2 | 3 × m6i.xlarge | 3 × m7i-flex.large | 账号是免费套餐，m6i.xlarge 不允许启动（节点组卡在 CREATING，权限检查查不出） | ✓ 01 §5.2、spike-results |
| 01 §5.2 | IRSA（默认做法） | Pod Identity | SCP 拒绝 `CreateOpenIDConnectProvider` | ✓ ADR-007 修订 |
| 08 M4 验收 | `eval run`、演示 | 在 EKS 上跑通一次调查，不跑评测 | 你的决定：上云这两天不以效果为目标 | ✓ 08 §3 |
| 08 §3 D13 | 相对 m3 零改动 | 相对 `m4-base` 零改动 | m3 的扩展点不完整，先做一次通用化（你的决定） | ✓ CLAUDE.md、spike-results D13 |
| CLAUDE.md | 相对 m4-base 零改动 | 5 条例外：exec/core.py（M3 审阅发现 1）；core/memory 同分排序、乐观锁；core/graph + remediation_store 同集群互斥、超时撤回；evidence.py 证据去状态前缀；core/store 去重窗口 | 审阅发现的正确性问题，你同意作为例外 | ✓ CLAUDE.md、02/05/06 实现说明 |
| 01 §5.2 | es-memory 在 kind 和 EKS 上都开安全功能 | EKS 上 ECK 只关 HTTP 层 TLS、认证照常，账号密码放在连接串里 | 记忆客户端没有认证参数，这样不用改 core | ✓ chart 注释、spike-results |
| 01 §3 | LangSmith 负责 Trace | 只设 key 不会上报，要显式 `LANGSMITH_TRACING=true` | 之前 CLI 的巡检一直没有 trace（D16 发现） | ✓ 01 §3 |
| 01 §6 | 执行器对 Postgres 只能读审批、写审计 | 和 Agent 用同一个超级用户 | M3 遗留，chart 照搬；T3 靠代码保证 | ✓ 01 §6 写明偏差（独立审阅发现 2） |

## 4. 简化和跳过的地方

人话结论：M4 新增 15 处 `TODO(edge)`（`git diff m3` 里新增的行）：部署层 6 处，例外修复的并发边界 8 处，多领域调查 1 处。真正要留意的是执行器网络策略的启动窗口和 Postgres 权限两处。

| 位置 | 内容 | 人话说明 | 影响 |
|---|---|---|---|
| `deploy/helm/sentinel/templates/netpol.yaml` | 新 Pod 启动头几秒策略未生效 | kind 实测：刚启动时外网通，15 秒后不通。EKS 上要彻底关掉需要 VPC CNI 的 strict 模式 | 低（执行器启动后不会主动连外网） |
| `deploy/helm/sentinel/templates/app.yaml` | 执行器用 Postgres 超级用户 | 见 §3 最后一行 | 中：审计表"只授予 INSERT"（M3 遗留）也一起没做 |
| `deploy/helm/sentinel/templates/app.yaml` | 观测原文存在 Pod 的临时目录 | Pod 重建就丢；要长期保留得挂卷或放 S3 | 低 |
| `deploy/helm/sentinel/templates/data.yaml` | 集群内连 es-memory 是明文 HTTP | 要加密得给记忆客户端加 CA 配置（改 core） | 低（只在集群内） |
| `deploy/terraform/aws/main.tf` | 不建 KMS key 加密 Secret | EKS 默认的 AWS 托管密钥加密仍然有 | 演示环境可接受 |
| `deploy/helm/sentinel/values.yaml` | kind 还在用旧的 manifest；mysql-target、Chaos Mesh 不在 chart 里 | 同一个 chart 两边用（ADR-007）只做了一半 | 中：kind 和 EKS 的配置可能慢慢走样 |
| `sdk/`（ReadOnlyHttp 和 MySQL 连接） | EKS 上的证书校验 | 仍然跳过校验（ECK 自签名） | 低（只在集群内） |
| `core/memory/`、`core/graph/remediate.py`、`governance/remediation_store.py`（8 处） | 例外修复的并发边界：记忆写回冲突没有退避和审计；两次调查同时确认同类新事故会各建一条；失效审计可能重复；调查进程崩溃时集群锁不释放；执行器侧不再查一次互斥；撤回没有单独的审计事件；plans 没有按 target 的索引 | 主路径正确，只在并发、崩溃时出现 | 低（现在只有一个巡检进程） |
| `core/graph/`（route 节点） | 一次调查涉及多个领域时合并结果 | 现在一次调查只属于一个专家包 | 低 |

## 5. 关键数字

人话结论：上云的费用很低（一次完整演示不到 1 美元），但这些数字只证明"能跑"，不证明"诊断得好"。

| 指标 | 数值 | 含义 | 怎么读 |
|---|---|---|---|
| EKS 一次完整演示（up → patrol → down） | 约 $0.7–1 | 按计费粒度估算，以 Cost Explorer 为准 | NAT、端点、控制面不足 1 小时按 1 小时算 |
| 悉尼每小时 | 约 $0.59 | 3 × m7i-flex.large + 控制面 + NAT + 端点 | 忘了 destroy 一个月约 $430；账号有每天 $8 的预算告警 |
| `aws-up` / `aws-down` 耗时 | 约 25 分钟 / 13 分钟 | 单次 | 最慢的是 EKS 控制面（约 9 分钟） |
| EKS 上的调查 | 12 秒，4 次工具调用，输入 10361 / 输出 781 token | 单次 | H2 才是注入的根因（H1 是"节点少一个"，data-2 还没回来），这次不评判 |
| MySQL dev / heldout top1 | 1.00 / 1.00 | 6 / 12 次运行 | 只有 3 个场景，dev 只有 1 个：只证明能跑通 |
| 测试数 | 251（M3 结束时 211） | — | — |

## 6. 护栏是否真的生效

人话结论：原有护栏抽查了 4 条，M4 新增的 3 条都故意违反了一次，全部拦住。违反都在临时 worktree 或模拟输入里做，主工作区没有动过。

| 护栏 | 故意违反的方式 | 结果 | 生效？ |
|---|---|---|---|
| 依赖方向 | 在 MySQL 专家包里 `from sentinel.core import detect` | `packs 只依赖 contracts 和 sdk` BROKEN：`sentinel.packs.mysql -> sentinel.core.detect (l.2)` | ✓ |
| 模型型号 | 在 `core/detect.py` 里写 `"gemini-3.7-flash"` | `test_src_has_no_model_ids` 失败 | ✓ |
| 契约修改 hook | 模拟编辑 `contracts/plan.py` 的事件 | exit 2，提示先问人；编辑 chart 文件 exit 0 | ✓ |
| （新）镜像对应一个提交 | 改了 `src/` 不提交 | `behavior_dirty = ['src/sentinel/core/detect.py']`；Dockerfile 的断言和 `aws-up` 的预检查都会拒绝 | ✓ |
| （新）执行器禁止外网 | 在 sentinel-exec 里起测试 Pod 访问外网 | kind：生效后 1.1.1.1、google 不通，mysql-target 通，es-memory 不通（对照组 sentinel-system 都通）。**EKS 上未测** | ✓（kind） |
| （新）凭证分开 | 渲染 chart，在 agent 的 Secret 里找写密码 | 找不到；写密码只在 sentinel-exec 的 Secret 里 | ✓ |
| MCP 目标白名单（D14） | 调用白名单外的目标 | 被拒，写审计 | ✓ |

## 7. 代码规模和需要人深读的模块

人话结论：M4 新增约 4000 行（含部署和测试，`m3..HEAD` +3986 / −458）。必须读懂的是"专家包怎么接进来"和"云上的凭证怎么分开"。

| 模块 | 代码行数 | 需要深读？ | 原因 |
|---|---|---|---|
| `core/packs_registry.py`、`sdk/connect.py` | 约 120 | ★ | 零改动扩展的全部机制：发现专家包、按连接类型建连接 |
| `packs/mysql/` | 406 | — | 第二个专家包，照着 ES 的样子写 |
| `governance/report.py` | 169 | — | 周报：每个数一条 SQL |
| `mcp/server.py` | 72 | ★ | 对外暴露的入口：只读、白名单、审计 |
| `governance/remediation_store.py`（M4 新增部分） | 约 130 | ★ | 同集群修复互斥（同一事务加锁） |
| `deploy/helm/sentinel/` | 471 | ★ | 凭证分开、网络策略、密码生成都在这里 |
| `deploy/terraform/aws/` | 227 | — | 关键在关掉的几个默认值（IRSA、KMS、日志组） |
| `deploy/aws/` | 224 | — | 尤其是 `aws-down` 先删 PVC 的顺序 |

## 8. 风险和下一步

人话结论：最担心的不是代码，而是"账号环境"——这次上云 3 次 apply 里有 2 次是被账号限制卡住的，而且这些限制事先查不出来。

- **账号限制在 apply 之前查不全**：SCP 的区域限制靠逐个区域试出来；免费套餐的实例类型限制连 dry-run 都查不出，只有真的启动实例才报错。以后换账号或升级套餐，先跑一次 `aws-up` 观察节点组。
- **免费套餐有期限和额度**：额度用完或到期，账号会被关闭，除非升级为付费套餐。
- **kind 和 EKS 的配置在分叉**：kind 还用旧 manifest，EKS 用 chart；这次执行器的网络策略就是两边各改一份。
- **执行器网络策略在 EKS 上没实测**：kind 上验证了；下次 `aws-up` 时应该顺手测一次。
- **之前的风险仍在**：场景太简单（M2 起）；临时修复不会自动撤销（M3 起）；审计表只授予 INSERT 没做（M3 起）。
- **08 §6.6 的固定动作**：模型漂移检查按你的决定记为缺口（和 M2、M3 一样）；08 §8 的工时只能按日历补（会话时间没有统计）。

## 9. 需要你决定的事

人话结论：有 3 件事需要你决定。**已决定（2026-11-20）**：v42 不评测；保持免费套餐。控制台访问还没决定。

| 事项 | 选项 | 影响 | 建议 |
|---|---|---|---|
| v42 要不要评测、上线 | A. 评测 es dev（和 v41 同标签配对，约 $1、30 分钟），按 06 §7.2 决定；B. 不评，上线版本留在 v41 | v42 包含证据检查去状态前缀等 5 条修复，A 能知道它们是否真的改善了证据支持率 | 建议 A。**决定：B**，上线版本留在 v41 |
| 控制台看集群里的资源 | A. 告诉我控制台的登录身份，写进 Terraform 的访问条目（`AmazonEKSAdminViewPolicy`，能读 Secret）；B. 不加，用 kubectl | A 每次 `aws-up` 自动生效 | **A**（如果登录身份不是 root） |
| 免费套餐 | A. 保持；B. 升级为付费套餐 | A 不能用 m6i 等实例类型，额度用完账号关闭；B 不能再回到免费套餐 | 建议 A。**决定：A** |

## 独立审阅（08 §6.5）

人话结论：审阅者是一个看不到写代码过程的子 Agent，读 `m3..HEAD` 的改动。结论是**各项任务都实现了，凭证分开和契约都符合设计**；报了 2 条，都成立，1 条已修、1 条按规则记录。

**审阅者逐项确认过的**：`make check`、`make demo-check`、`helm template` 都通过；14 个契约文件的语法树和 m3 相同（契约没改）；`m4-base..HEAD` 下 core/exec 除 5 条例外只有注释；agent 的 Secret 只有只读账号；审批 ID 由代码生成；MCP 只导出只读工具、不导出动作；MySQL 动作参数先过 schema 再限定不超过原值 2 倍。

| # | 发现 | 复核 | 处理 |
|---|---|---|---|
| 1（中） | **执行器没有网络策略**：01 §5.1、ADR-005、07 §3 T6 都要求执行器禁止外网；kind 的 manifest 从 M3 起写着"以后补"，chart 照搬了缺口，EKS 上有 NAT，等于能上外网 | 成立 | **已修**：chart 和 kind 都加了 `exec-isolation`（入站全拒；出站只到 targets、Postgres 5432、DNS）。kind 上实测生效（§6），并发现启动头几秒的窗口（§4） |
| 2（低） | **执行器用 Postgres 超级用户**，能写 `approval_decisions`，超出 01 §6 的矩阵 | 成立（M3 遗留） | 按"只实现主路径"记 `TODO(edge)`，01 §6 写明偏差 |

## 术语表

| 术语 | 白话解释 |
|---|---|
| 专家包 | 一个领域（ES、MySQL）的全部知识：工具、检测器、动作、场景、runbook。加新领域只加一个目录 |
| 零改动验收 | 加 MySQL 时 core 和执行器的代码一行不改，用来证明扩展点设计对了 |
| `m4-base` | 为了让零改动成立，先做的一次"通用化"提交；零改动从它开始算 |
| MCP | 一种让外部 Agent 调用工具的标准协议；这里只暴露只读工具 |
| SCP（服务控制策略） | AWS 组织给成员账号加的限制，优先级高于账号自己的权限设置 |
| 免费套餐（Free account plan） | AWS 新账号的一种套餐：用赠送额度，只能启动部分实例类型，额度用完账号关闭 |
| IRSA / Pod Identity | 两种让 Pod 拿到 AWS 权限的方式；IRSA 要建 OIDC provider（被 SCP 禁止），Pod Identity 不需要 |
| tainted | Terraform 对"创建了一半"的资源的标记，下次 apply 会先删掉再重建 |
| NetworkPolicy | K8s 的防火墙规则：哪些 Pod 能连哪里 |
| initContainer | Pod 里先于主容器运行的容器，这里用来"等数据库建好表" |
| access entry / 访问策略 | EKS 里决定"哪个 AWS 身份在集群里能做什么"的设置，和 IAM 策略是两层 |
| 漂移检查 | 用当前数据集重跑上一个里程碑的上线版本，看模型本身有没有变 |
