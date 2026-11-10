# 技术验证结果（Day 0，2026-11-09）

> 对应 08 篇 §1。每项写：结论、实测数字、需要改动的设计。验证代码在 `spike/`，用完即弃。
> 环境：MacBook 16 GiB / 8 核；Docker Desktop 分配 9.7 GiB / 4 核 / VM 磁盘 40 GB；kind v0.33.0（K8s v1.37.0）；ECK 3.5.0；ES 9.5.3；Chaos Mesh 2.8.6。
> 选版本的规则：只用发布满两周的版本（ES 9.5.5、Chaos Mesh 2.8.7/2.8.8 因此没有用）。

## 总览

| # | 结论 | 一句话 |
|---|---|---|
| S1 | 通过 | 3 节点 × 1Gi heap 能跑，内存有余量；`monitor` + `view_index_metadata` 够用；**kindnet 会执行 NetworkPolicy** |
| S2 | 通过（5/6） | 5 种故障 3/3 次在 60 秒内出现症状、1 分钟内恢复；**CPU 热点在 kind 上做不出来**（CPU 指标是整个 VM 的） |
| S3 | 通过 | 重启后从审批处恢复，diagnose 不重跑；子 Agent 原始输出不回流；`/obs/` 映射可行 |
| S4 | 通过（有 2 处要改设计） | 标签要按版本时间打；`dataset_version` 这个 metadata 键会被 SDK 覆盖 |
| S5 | 按降级方案通过 | **basic 授权下服务端 RRF 不可用（403）**，改为客户端融合 |

另外确定了模型型号（ADR-008）：诊断 `gemini-3.7-flash`，评审 `gemini-3.1-pro-preview`，embedding `gemini-embedding-001`（768 维）。

---

## S1 kind + ECK + ES 9 三节点；只读权限；NetworkPolicy

**结论：通过。**

| 检查项 | 结果 |
|---|---|
| ES 版本 | ECK 3.5.0 部署 ES 9.5.3 成功，`health=green`，3 节点 |
| 内存 | 3 节点 × 1Gi heap（容器上限 2Gi）：启动后 Docker VM 已用约 6.1 / 9.7 GiB；装上 Chaos Mesh、跑完全部故障后约 6.4 GiB（worker 3.4G + worker2 1.9G + 控制面 1.1G），余量约 3.3 GiB > 2 GiB 的要求 |
| 稳定性 | 集群运行 28 分钟以上（期间跑了 21 次故障注入），data-0、data-1 重启 0 次。data-2 有 2 次 OOMKilled，都是 S2 在 ES 容器里写盘造成的（见 S2），不是正常负载导致的 |
| 只读用户 | 10 个读 API 全部 200；写文档、改集群设置、删索引全部 403 |
| NetworkPolicy | 默认的 kindnet **会执行**：有策略时从 `other` 命名空间访问超时，从 `sentinel-system` 访问能连通（401，说明只是缺凭证）；去掉策略后 `other` 也能连通（对照） |

**只读角色的准确定义**（01 §6 原来写的是【据记忆】）：

```json
{"cluster": ["monitor"], "indices": [{"names": ["*"], "privileges": ["monitor", "view_index_metadata"]}]}
```

通过的读 API：`_cluster/health`、`_cat/shards`、`_cluster/allocation/explain`、`_nodes/stats`、`_nodes/hot_threads`、`_cluster/settings`、`<index>/_settings`、`_cat/indices`、`_tasks`、`_cat/recovery`。

**附带发现**：这个角色**读不到文档内容**（`<index>/_search` 返回 403）。这是好事：诊断 Agent 只能看到元数据和统计信息，碰不到业务数据，07 篇的数据出境风险也因此变小。如果以后有工具确实需要读文档（例如读慢查询样本），要单独给那个索引加 `read`，并且写进 ADR。

**本机的一个坑**：本机 kubectl 的默认 context 指向另一个集群。所有脚本都显式写了 `--context kind-sentinel`，`make kind-up` 也要这样做。

## S2 Chaos Mesh 能注入哪些故障

**结论：通过（5 种 ≥ 4 种）。** 所有故障都打在 `data-2` 上，每种跑 3 次；探针用只读用户通过 `data-0` 轮询，判断条件和设计中的检测器一致。原始结果在 `spike/s2/results.json` 和 `results-disk.json`。

| 故障 | 注入方式 | 症状（探针） | 出现症状（秒，3 次） | 撤销后恢复 green（秒） | 结论 |
|---|---|---|---|---|---|
| es.disk_watermark | 脚本：在 kind 节点上往 data-2 的数据盘写文件到 93%，再创建一个副本数为 2 的新索引 | 磁盘超过 90%，接着出现未分配分片 | 9.0 / 16.1 / 12.9（未分配分片几乎同时出现） | 立即 | 通过 |
| es.node_down | Chaos Mesh PodChaos `pod-failure` | 节点数 < 3 | 52.7 / 52.2 / 50.7 | 25 / 46 / 56 | 通过 |
| es.config_change | 脚本：`cluster.routing.allocation.enable=none`，然后重启 data-2 | 出现未分配分片 | 50.9 / 52.5 / 50.5 | 36 / 20 / 25 | 通过 |
| es.shard_limit | 脚本：把 `cluster.max_shards_per_node` 调到刚好等于当前分片数，再创建索引 | 分片数 ≥ 上限的 90%；创建索引返回 400 `this action would add [9] shards, but this cluster currently has [15]/[15]` | 0.1 / 0.0 / 0.0 | 立即 | 通过（见下面的注意事项） |
| es.network_partition | Chaos Mesh NetworkChaos `partition`（双向） | 节点数 < 3 | 34.3 / 34.3 / 34.3 | 5 / 5 / 5 | 通过 |
| es.hot_node_cpu | Chaos Mesh StressChaos（CPU，2 个 worker × 100%） | data-2 的 `os.cpu.percent` ≥ 80 | — | — | **不通过** |

**CPU 热点为什么不通过**：压 data-2 时，3 个 ES 节点报告的 `os.cpu.percent` **完全相同**（基线 13%，压测时都是 56%），而 ES 进程自身的 `process.cpu.percent` 没有变化。kind 的所有节点共用一台 Docker VM，`os.cpu` 读到的是整个 VM 的 CPU，StressChaos 又只增加旁路进程的 CPU。所以在 kind 上做不出“单个节点 CPU 高”这种症状。第一轮跑批里第 2、3 次“0 秒检测到”也是这个原因造成的假阳性。要模拟热点，就得用真实的查询负载去打 data-2 上的分片，让 ES 进程自己的 CPU 升高。这放到 M2 扩充场景时再做（`# TODO(edge)`）；M1 的 4 种故障先从上面 5 种里选。

**注意事项（D4 写故障台时用）**：

1. **es.shard_limit 的注入方式会留下线索**：调低 `max_shards_per_node` 会在集群设置里留下一个明显的异常值，诊断 Agent 直接看设置就能猜到答案。正式场景应该保持默认上限，改为创建大量小索引把分片数推到上限。
2. **es.config_change 本身就是“设置变更”类故障**，设置里留下线索是这个场景应有的样子。
3. **磁盘填充不能在 ES 容器里做**：在容器里 `dd` 写入（加 `oflag=direct` 也一样），写到约 1GB 时 ES 容器就会被 OOMKill（exit 137）。我判断原因是 loop 设备经过页缓存写底层文件，这部分内存记在发起写入的容器的 cgroup 上。所以改为在 kind 节点上（`docker exec <node>`）往同一个目录写，故障含义不变。另外 ES 镜像（RHEL 9）里没有 `fallocate`。
4. **磁盘场景要先等 ES 看到超水位，再触发新的分配**：ES 每 30 秒刷新一次磁盘信息。如果新索引在刷新之前创建，副本仍然会被分到 data-2 上，导致“未分配分片”时有时无（第一轮观察到 1 次出现、1 次没出现）。改成先等探针确认超水位后，3/3 次稳定出现。
5. 各故障的 `symptom_wait.timeout_s` 可以从 300 秒收紧到 120 秒（实测最慢 53 秒，留 2 倍余量）。

**已经确认的磁盘问题**：kind 的 local-path 卷就在 Docker VM 的共享盘（`/dev/vda1`，40G）上，直接 `fallocate` 会把整个 VM 的磁盘写满。解决办法：在每个 worker 节点上用 loop 设备建一个 3G 的 ext4，做成静态 local PV（StorageClass `es-fixed`，见 `spike/s2/fixed-pv.yaml`）。ES Pod 里看到的数据盘是 2.9G 的 `/dev/loop0`，填盘只影响这一块。`make kind-up` 要包含这一步。

## S3 LangGraph 暂停恢复 + Deep Agent 节点 + 子 Agent 隔离 + `CompositeBackend`

**结论：通过（8/8 检查项）。** 版本：deepagents 0.7.34、langgraph 1.2.20、langchain 1.4.9、langchain-google-genai 4.4.3、langgraph-checkpoint-postgres 3.2.0。

做法：一个最小图 `diagnose`（节点内调用 `create_deep_agent`）→ `approve`（只有 `interrupt()`）→ 结束；用 FastAPI 包装，`thread_id` 等于调查 ID，checkpointer 用 `PostgresSaver`。

| 检查项 | 结果 |
|---|---|
| 启动调查 | 返回 `status=interrupted`，payload 里带诊断结果（约 22 秒） |
| 杀掉进程、重启后恢复 | 返回 `status=done`，审批决定已经写入状态 |
| diagnose 是否重跑 | `diagnose_runs` 计数 = 1，没有重跑 |
| Gemini 工具调用 | 主 Agent 的调用顺序：`es_cluster_health` → `read_file` → `task` |
| `/obs/` 映射 | `CompositeBackend(default=StateBackend(), routes={"/obs/": ObsBackend()})` 可行，`read_file` 读到了自定义后端里的标记 |
| 子 Agent 隔离 | 子 Agent 的工具输出里有 50 行带标记的原始内容；主 Agent 的 messages 里**没有**这个标记，只有子 Agent 交回的一句结论和 `obs_42` |

**写实现时要注意**：

1. `CompositeBackend` 会先去掉路由前缀再调用后端（`/obs/obs_1.json` 到后端时是 `/obs_1.json`）。自定义后端只需要实现 `read`、`ls`、`grep`、`glob`；写操作直接拒绝，保证观测记录只读。
2. **框架自带的摘要中间件默认开启**，阈值从模型 profile 推算：上下文用到 `max_input_tokens` 的 85% 才触发（gemini-3.7-flash 约 89 万 token）。我们 L2 的上限是 2 万 token，永远到不了这个阈值，所以和 G3 的自控压缩（04 §3.11）不冲突，也不用关掉。
3. **思考 token 计入 `max_output_tokens`**：gemini-3.7-flash 在 `max_output_tokens=200` 时，思考用掉 188 个，回答被截断（`finish_reason=MAX_TOKENS`）；设 `thinking_budget=0` 后正常输出。所以 `thinking_budget` 要作为模型参数写进 AgentVersion，`core/llm` 的预算也要把思考 token 算进去。
4. 本次只跑了 1 次调查（n=1）。子 Agent 隔离是框架的结构性行为，不依赖 LLM 的随机性；工具调用顺序则依赖提示词，D3 的流程测试会用假模型覆盖。

## S4 LangSmith

**结论：通过，有 2 处要改设计。** 版本：langsmith 0.10.10。

| 检查项 | 结果 |
|---|---|
| 版本标签 + split | `as_of="ds-spike-1", splits=["dev"]` 取到 2 条，heldout 1 条，全部 3 条；`ds-spike-2` 全部 4 条，dev 3 条，全部符合预期 |
| `evaluate()` 参数 | `num_repetitions=3`、`metadata={...}`、`experiment_prefix=...` 都可以用 |
| 按版本筛选 | `list_projects(reference_dataset_name=..., metadata={"agent_version": "spike-a"})` 只返回 `spike-a-18432f70` |
| 通过 SDK 拉取 | 每个用例都有 3 次运行 + 3 条 feedback |
| 缓存 | 没有设置 `LANGSMITH_TEST_CACHE`；3 个用例的 3 次重复全部是 3 种不同的输出（temperature=1.0），没有被缓存 |

**要改的设计**：

1. **标签要按数据集版本的时间打。** 添加用例后，`dataset.modified_at` 不会前进，用它打标签会指向一个没有用例的版本，查询结果全部为 0。正确做法是用 `list_dataset_versions()` 返回的最新 `as_of`。D4 的 `dataset sync` 要照这个做法实现。
2. **06 §9 里的 metadata 键要改名，比如改成 `dataset_tag`。** SDK 会自己写入 `dataset_version`（值是时间戳），覆盖我们传入的 `ds-spike-2`。时间戳也要保留，它能精确定位数据集版本。

另外，SDK 会自动把 git 信息（commit、branch、dirty）写进实验的 metadata，可以和 AgentVersion 的 `code_sha` 互相校验。

## S5 es-memory：RRF 和 embedding

**结论：按降级方案通过。** ES 9.5.3 单节点，`_license` 的类型为 `basic`。

| 模式 | 结果 |
|---|---|
| 服务端 RRF（05 §3.3 原文，带 `weight`） | **403**：`current license is non-compliant for [Reciprocal Rank Fusion (RRF)]` |
| 服务端 RRF（不带 `weight`） | 同上，403 |
| 客户端融合（3 次查询，`Σ w/(60+rank)`，指纹那一路的权重为 2） | 200，top-5 = `mem_disk, mem_nodeleft, mem_alloc_off, mem_hot, mem_heap`；正确答案排第 1；另一个 namespace 的记录被过滤掉 |

**Embedding**：

| 模型 | 默认维度 | 指定 768 维 | 单次延迟 |
|---|---|---|---|
| gemini-embedding-001 | 3072 | 可以 | 约 0.4–0.5 秒 |
| gemini-embedding-2 | 3072 | 可以 | 约 0.5 秒 |

选 `gemini-embedding-001`、768 维：只有文本输入，用不到 `-2` 的多模态；768 维的索引体积是 3072 维的 1/4，在几百条记忆的规模下召回差异可以忽略【推断，M2 可以用 M-E1 验证】。价格本次没有查到，在 M2 的成本统计里补上。

**附带发现**：spike 里的 `symptom_text` 用的是 standard 分词，中文按单字切分，所以文本召回比较粗。决定（2026-11-10）：`symptom_text` 的模板改用英文，standard 分词即可（05 §3.1）。

---

## 需要写回的设计改动

下面的改动都已经写入对应篇章。03 篇的改动于 2026-11-10 确认，记录在 ADR-009。

| 位置 | 改动 |
|---|---|
| 01 §5 | 本地资源改成实测数字；`make kind-up` 加上固定大小的 PV 和显式 context |
| 01 §5.1 | 删掉“需要换成 Calico 或 Cilium”；kindnet 会执行策略 |
| 01 §6 | 只读权限改为实测的准确定义；补上“读不到文档内容” |
| 03 §3.1 | 已确认：`supports` 去掉【待验证】，写上实测版本 ES 9.5.3 / ECK 3.5.0 |
| 00 §5 | “ECK 支持 9.x”去掉【据记忆】 |
| 04 §3.3 | `/obs/` 映射去掉【待验证】；子 Agent 隔离由【推断】改为“S3 已确认” |
| 04 §3.11 | 框架自带的摘要中间件：阈值是 85%，和 G3 不冲突 |
| 04 §3.5、05 §3.3 | RRF 改为客户端融合；删掉服务端查询示例，写上客户端融合的算法 |
| 05 来源说明 | embedding 的维度改为实测 |
| 06 §9 | `metadata` 和 `experiment_prefix` 去掉【待验证】；键名改为 `dataset_tag`；补上“按版本时间打标签” |
| 03 §5.2 | 已确认：在 `models` 的注释里写明参数要包含 `thinking_budget`（不新增字段，`models` 本来就是 dict） |
| 03 §6.1 | 已确认：示例里的 `symptom_wait.timeout_s` 从 300 改为 120；`fault.ref` 的磁盘脚本改为在节点上执行；干扰项换成网络延迟；节点宕机和网络分区统一标签 `es.node_unavailable` |
| 08 §7 | 风险表补上“CPU 热点在 kind 上做不出来” |
| ADR-008 | 写上具体型号 |
| 01 §5 | Docker 调到 12 GiB；es-memory heap 512Mi；全链路内存在 M1 D1 实测后补到本文件 |
| 04 §6 | 第一版不提供慢查询日志工具 |
| 06 §5.3 | 说明为什么节点宕机和网络分区用同一个标签 |
| 08 M1 D1 | `make kind-up` 用脚本建 loop 虚拟盘（可重复执行）；填盘脚本的安全检查；spike 代码移到 `deploy/` |
| 08 M2 D8、§7 | 加 2 种性能类故障；风险表补“故障集中在分配类” |
| 08 §6.4、ADR-008 | 模型型号只在 `config/models.yaml` 里出现，测试扫描 `src/` |

## M1 D1 全链路内存（2026-11-10）

环境：Docker Desktop 12 GiB（VM 内可见 11.4 GiB）/ 4 核。`make kind-up` 完成后稳定 15 分钟再测，命令是 `deploy/kind/mem_report.sh`。26 个 Pod，0 次重启，0 次 OOM。

| 项 | 实测 |
|---|---|
| Docker VM 总体 | 已用 7958 MiB / 11697 MiB，可用 3738 MiB |
| kind 节点 | control-plane 1.35 GiB · worker 3.37 GiB · worker2 2.81 GiB |
| es-target ×3（heap 1g，上限 2Gi） | 每个约 1.49–1.56 GiB |
| es-memory（heap 512m，上限 1Gi） | 915 MiB |
| postgres | 23 MiB |
| ECK operator + Chaos Mesh（全部组件） | 约 0.2 GiB |
| kube-apiserver | 690 MiB |
| agent | 未部署（D3 起才有镜像），到时补测 |
| mysql-target | 未部署（M4） |

**结论**：余量约 3.7 GiB，高于 1.5 GiB 的降级线，es-target 保持 1g heap。agent 预计不超过 1 GiB，加上之后仍有余量。

**要注意**：es-memory 的工作集 915 MiB，已经接近 1Gi 的上限。其中包含可回收的页缓存，所以不一定会 OOM。但记忆写多了以后要观察；出现 OOMKilled 时，先把上限调到 1.5Gi，heap 不变。

**D1 顺带确认**：
- **声明式只读用户可用**：ECK 的 `spec.auth.roles` 加上 basic-auth 类型的 fileRealm Secret。`deploy/kind/ro_check.sh` 的 14 项全部通过：10 个读请求返回 200，4 个写入或读文档的请求返回 403。
- **`kind-up` 可以重复执行**：第二次运行时，每一步都显示“已存在，跳过”；`kubectl diff` 为空，Pod 没有重启。
- **填盘的百分比要按 ES 的口径算**：ext4 默认保留 5% 给 root，`df` 的 used 列不含这部分，ES 却把它算作已用。第一次按 used 列计算，结果填到了 99%（越过 flood stage）。改成 1 − 可用/总量之后，ES 看到的是 93%。撤销后恢复到 5%，集群 green。
- **安全检查**：对非挂载点（`/var/lib`）和非 loop 设备的挂载点（节点根目录，挂载源是 overlay），都拒绝执行，exit 3。

## M1 D2 发现（2026-11-10）

在 kind 上制造 4 种状态，录制成测试数据 `tests/fixtures/es/*.jsonl`，来源是 ES 9.5.3 的真实输出。

| 状态 | 制造方式 | 触发的检测器 |
|---|---|---|
| healthy | — | 无 |
| disk_watermark | `fill_disk.sh --pct 93`，等 40 秒后创建 `bench-new`（1 主分片 2 副本） | `es.disk_watermark`、`es.unassigned_shards` |
| node_down | `pod_failure.yaml` | `es.node_count`、`es.unassigned_shards` |
| shard_limit | 把 `cluster.max_shards_per_node` 调到 5 | `es.shard_limit`（critical） |

1. **没有业务索引时，节点宕机不会留下未分配分片**。系统索引都设了 `auto_expand_replicas`，节点少了就自动减副本，60 秒后未分配分片归零，只剩"节点数减少"一个症状。所以 `kind-up` 增加了种子数据这一步：`deploy/chaos/es/seed.sh` 创建 `bench` 索引，3 个主分片、2 个副本、2000 条文档。
2. **磁盘填满后，要等 ES 刷新磁盘信息（约 30 秒）再建索引**。和 S2 的结论一致：分配决策用的是每 30 秒刷新一次的磁盘快照，而 `_cat/allocation` 读的是实时值。第一次在 `_cat/allocation` 显示 92% 后立刻建索引，副本仍被分到了 data-2。
3. **ECK 默认开启了分配感知**（`node.attr.k8s_node_name`）。`allocation_explain` 里除了 `disk_threshold`，还会出现 `awareness` 拒绝原因。这对诊断是有用的上下文，也是一个潜在的干扰项。
4. **port-forward 到 Service 时，可能恰好连到被注入故障的 Pod**。节点宕机期间连接会断。D4 的 bench 要像 S2 那样，固定转发到不被注入故障的节点。
5. **`_nodes/stats` 的大小**：3 个节点全量约 230 KB；只取 5 类指标约 27 KB，每个节点约 9 KB。摘要后约 76 个 token。
6. **两个摘要器根据真实数据做了修正**：
   - `hot_threads` 的标题行带着整串节点属性，现在只保留节点名；
   - `health-node[c]` 这类持久任务一直在运行，不应算作"长时间运行的任务"。

## M1 D3 发现（2026-11-10）

**第一次接入真实的 Gemini**。诊断模型 `gemini-3.7-flash`，回放 `disk_watermark` 录制：

| 版本 | 改了什么 | 次数 | H1 正确 | 证据支持率 | 工具调用 | token（输入 / 输出） |
|---|---|---|---|---|---|---|
| v1 | 首个版本 | 1 | 1/1 | 67% | 2 | 9.9k / 0.5k |
| v2 | 提示词：引文只能摘自工具输出 | 3 | 3/3 | 100%、100%、100% | 2、2、3 | 约 10k / 0.5–0.6k |
| v3 | 代码：上传 trace（行为不变） | 1 回放 + 1 kind 在线 | 2/2 | 100% | 2 | 10.7k；在线 15.2k / 0.4k |

LangSmith 记录的费用约为每次调查 0.008 美元。样本只有几次，这些数字只是 D4 评测前的初步观察，**不是结论**。

1. **v1 证据支持率只有 67%，原因是引文的来源不对**。模型把 L0 问题事件的说明文字（"磁盘使用率 92%，超过高水位 90%"）当成引文，但这句话是检测器生成的，不在任何观测摘要里，确定性检查因此判它不通过。检查本身是对的，所以修的是提示词：引文只能从工具输出里逐字摘取，L0 和 L1 只引用观测 ID。v2 之后 3 次都是 100%。
2. **结构化输出用 `response_format=ToolStrategy(DiagnosisOutput)` 是可行的**：Gemini 配合 deepagents 0.7.32 时，5 次都能稳定给出结构化结果，不需要退回 `submit_diagnosis` 工具。
3. **CLI 进程退出时会丢 trace**：前 3 次调查的 trace 没有上传到 LangSmith，因为 trace 在后台线程上传，进程先退出了。改为在巡检结束时调用 `wait_for_all_tracers()`；外层调查也打上 `investigation_id` 标签。
4. **依赖版本比 S3 低一个小版本**：deepagents 0.7.34 是 2026-11-05 发布的，不满两周，被 `exclude-newer` 挡住了。实际使用的是 deepagents 0.7.32、langgraph 1.2.18、langchain-google-genai 4.4.2。S3 验证过的三项行为，D3 的测试和真实运行都再验证了一遍：
   - `CompositeBackend` 能把 `/obs/` 映射到自定义后端；
   - 工具调用正常；
   - checkpointer 用调查 ID 作为 thread_id。
5. **在线全链路**：在 kind 上注入磁盘故障，用 live 后端巡检，诊断正确，证据支持率 100%，用时 10.7 秒。注入前巡检是健康的，用时 1.2 秒，没有调用模型。`POST /patrol` 同样能用。

## M1 D4 结果（2026-11-10）：M1 验收跑通

**录制**：4 个单一故障场景各录 2 条，共 8 条，全部在 dev split。每条录制都把只读工具全部扫了一遍，不调用 LLM，每条 30–43 KB。

| 场景 | 症状出现时间（从开始注入算起） |
|---|---|
| es.disk_watermark.single | 54.5 秒、54.3 秒（含 40 秒等 ES 刷新磁盘信息） |
| es.node_down.single | 56.1 秒、50.9 秒 |
| es.allocation_disabled.single | 77.6 秒、81.7 秒（要等 data-2 重新加入集群） |
| es.shard_limit.single | 1.4 秒、1.6 秒 |

**评测**：数据集标签 `ds-20261110-1`，v6（`gemini-3.7-flash`），每个用例重复 3 次，共 24 次运行。

| 场景 | top1 | 证据支持率 | 引用了必需工具 | 上下文召回 | 平均工具调用 | 平均 token |
|---|---|---|---|---|---|---|
| allocation_disabled | 1.00 | 0.85 | 1.00 | 1.00 | 3.2 | 17.2k |
| disk_watermark | 1.00 | 1.00 | 1.00 | 1.00 | 2.8 | 10.8k |
| node_down | 1.00 | 0.89 | 0.83 | 1.00 | 3.0 | 11.3k |
| shard_limit | 1.00 | 0.78 | **0.50** | 1.00 | 2.2 | 13.8k |

合计费用 0.27 美元（按估算单价，见下面第 4 条）。录制未覆盖 0 次，不稳定用例率 0。

**比较**（主指标 top1，按场景聚类的配对 bootstrap，4 个场景）：

| 比较 | 差值 | 95% 区间 | 判断 |
|---|---|---|---|
| v6 − 纯规则 | +0.00 | [+0.00, +0.00] | 无法判断 |
| v6 − 多数类 | +0.75 | [+0.25, +1.00] | 提升 |

**怎么读**：
1. **在单一故障上，纯规则基线已经是满分**，这和 08 §7 风险表的预判一致。M1 的数据不能说明 LLM 比规则好，只能说明 Agent 没有比规则差，而且每个结论都给出了可以检查的证据。LLM 的价值要到 M2 的"有干扰项"和"组合故障"场景里去证明（06 §4.5）。
2. **只有 4 个场景，区间的意义有限**。M1 证明的是"录制 → 同步 → 评测 → 比较"这条流程能完整跑通，不能证明 Agent 好不好。
3. **shard_limit 有一半的运行没有给出通过检查的 `es.cluster_settings` 证据**。根因判断对了，但证据链偏弱，这是 M2 失败归类要先看的地方。
4. **诊断模型的价格**：Google 官方价格页（2026-11-10 查询）没有列出 `gemini-3.7-flash`，只列了同价的 3.6-flash 和 3.8-flash（每 100 万 token 输入 0.75 美元、输出 3.75 美元，含思考 token）。`config/pricing.yaml` 按相邻版本估算，实验的 metadata 里标了 `pricing_basis: estimated`。模型接口仍然提供 3.7-flash（版本 3.7-flash-08-2026）。
5. **重复同步也会生成新版本**：用例内容没有变化时，`update_examples` 同样会产生一个新的数据集版本，于是多出一个标签（`ds-20261110-2`，内容和 `-1` 相同）。没有影响，但变更日志里多了一行。

## M2 第一段（2026-11-10）：场景扩充，第一次在规则答不对的场景上比较

**换模型的决定**：评估后决定保留 `gemini-3.7-flash`（ADR-008）。3.8-flash 的 version 字段是"3.0"，没有带日期的发布号，无法确认是固定版本。

**M2 前置修复**：token 预算改为中途中止（v8）；30 分钟指纹去重；外部改编的场景一律进 heldout（06 §5.2）。

**场景**：17 个，每个 2 条录制，标签 `ds-20261110-4`。
- dev 12 个：single 4、distractor 6、compound 2；
- heldout 5 个：hash 分到的 2 个，加上来自 Elastic 官方文档的外部改编 3 个。heldout 按规则留到 M2 验收时再跑。

**第一次评测时，24 条用例里有 7 条因为录制未覆盖被剔除**（v9，`ds-20261110-3`）。9 次 replay_miss 全部是 Agent 追问具体分片的 `allocation_explain {index, shard, primary}`，而录制只录了默认参数的调用。M1 报告的"简化和跳过"里写过这一条，M1 的场景太简单，没有碰到。修法：录制时为每个未分配分片都录一次带参数的调用，然后全部重录。重录后录制未覆盖为 0。

**v10 在 dev 上的结果**（`ds-20261110-4`，24 个用例 × 3 次 = 72 次运行，0.90 美元，按估算单价）：

| 分组 | 纯规则 | v10 | 差值 | 95% 区间（按场景） | 判断 |
|---|---|---|---|---|---|
| 全部（12 个场景） | 0.83 | 0.92 | +0.08 | [-0.17, +0.33] | 无法判断 |
| single（4） | 1.00 | 1.00 | +0.00 | [0, 0] | 无法判断 |
| distractor（6） | 0.83 | 0.83 | +0.00 | [-0.50, +0.50] | 无法判断 |
| compound（2） | 0.50 | 1.00 | +0.50 | [0, +1.00] | 无法判断 |
| 对比多数类（全部） | 0.33 | 0.92 | +0.58 | [+0.33, +0.83] | 提升 |

不稳定用例率 0：每个用例的 3 次运行结果完全一致。

**纯规则和 LLM 分别在哪里错**：

| 场景 | 纯规则 | v10 | 发生了什么 |
|---|---|---|---|
| allocation_disabled.compound | 0/2 | 6/6 | 规则看到磁盘检测器触发，就答成磁盘水位。Agent 读 allocation_explain，发现每个节点都是被 `enable` 决策器拒绝，判断出磁盘告警是红鲱鱼 |
| replicas_unassignable.with_distractor | 0/2 | 6/6 | 规则表里没有这种根因，只能按"只有未分配分片"答成 allocation_disabled |
| **shard_limit.with_distractor** | 2/2 | **0/6** | Agent 把集群 yellow 的原因解释对了（干扰索引 `archive-2025` 的副本放不下），但**完全忽略了 critical 级别的 `es.shard_limit` 问题事件**。6 次都只给了 1 个假设，所以 top-3 也拿不到分。干扰项本身也是一个真实的异常，Agent 抓住了能看到的症状，漏掉了最严重的那个问题 |

**怎么读**：
1. **LLM 赢的地方和事先预测的一致**：要读证据才能排除红鲱鱼的场景，以及规则表没覆盖到的根因。**LLM 输的地方没有被预测到**：同时有两个问题时，它只解释了一个。
2. **区间仍然包含 0**：12 个场景太少，06 §4.3 的模拟显示需要 0.15 以上的提升才能稳定检测到。现在的 +0.08 是一胜一负相抵之后的结果，**不能说 LLM 比规则好**。
3. **M2 失败归类的第一条线索**：诊断没有覆盖所有 critical 的问题事件，而且只给了一个假设。可能的修法：在提示词里要求每个 critical 问题事件都要被某个假设解释，或者被明确排除；或者升级规则改为"有 critical 问题事件没被解释就升级"。这个改动要作为一个独立版本来评测。
4. **性能类故障没做**：3G 的盘、1G 的 heap 装不下足以触发熔断器的真实数据，ES 先用 `search.max_buckets`（65536）挡住了请求；写入队列默认 10000，也打不满（06 §5.3）。

**录制中发现的问题**：
- 干扰项 X3（`update_by_query`，`wait_for_completion=false`）会让 ES 创建隐藏的 `.tasks` 索引，这个索引不会消失。所以 X3 第一次注入之后，"健康"状态的活跃分片数从 15 变成了 17。这符合真实情况，而且所有脚本都按当前的分片数计算，不受影响。
- 注入做到一半失败时，原来不会撤销。一次失败的 `capacity-filler` 索引残留，污染了 `shard_limit.single#r3`。已修复，这条录制已作废并重录。
- **未解决**：`shard_limit.sh` 带 `TARGET_RATIO` 注入时，在故障台里失败了两次（exit 1，stderr 为空），手动执行没能复现。现在注入失败会自动撤销，所以不会留下残留。

