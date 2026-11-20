# ADR-002 用 FastAPI 托管调查图，不依赖 LangGraph Server

- 状态：已接受（2026-11-09）；前提经 S6 确认（2026-11-19）：自托管要设 `LANGGRAPH_CLOUD_LICENSE_KEY`，需要授权
- 相关：01 篇结论第 5 条

## 背景

LangGraph 的图需要一个服务进程来托管：接收 webhook、处理审批之后的恢复、执行后台任务。

## 决策

用 FastAPI 包装编译好的图，checkpointer 使用 `langgraph-checkpoint-postgres`。调试时可以使用 LangGraph Studio，但生产环境的运行不依赖 LangGraph Server。

## 放弃的方案

| 方案 | 放弃原因 |
|---|---|
| LangGraph Server 自托管 | 自托管需要授权（S6 确认：官方文档要求 `LANGGRAPH_CLOUD_LICENSE_KEY`，启动时联网校验）；另外，托管层的内部机制被封装起来，在面试中不好讲清楚 |
| LangSmith 托管部署 | 被管系统在 VPC 内网，执行器必须在内网；托管服务和内网之间的连通性会增加复杂度 |

## 后果

- 好处：没有授权方面的依赖；恢复、并发、后台任务的实现完全可控、可以讲清楚。
- 代价：需要自己实现几个简单的接口（触发调查、查询状态、审批之后恢复）。不过这些接口本来就是 CLI 需要的。
