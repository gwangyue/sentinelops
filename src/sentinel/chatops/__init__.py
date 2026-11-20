"""飞书 ChatOps（09 篇；ADR-015）：CLI 之外的第二个入口，安全模型不变。

负责：推送（notifier，D18）、卡片审批（D19）、固定指令（D20）。
不负责：任何审批规则和执行——都调用 CLI 背后的同一批函数；不 import `sentinel.exec`（.importlinter）。
"""
