"""MySQL 的阈值（05 §3.5"绝对阈值由专家包定义"）：检测器、事实卡片、动作的前置检查共用，标准一致。"""

CONN_WARN = 0.90  # 已用连接 / max_connections
CONN_CRITICAL = 0.98
LOCK_WAIT_S = 10  # 有事务等锁超过这么多秒就报警


def usage(status: dict[str, str], variables: dict[str, str]) -> float:
    return int(status["Threads_connected"]) / max(1, int(variables["max_connections"]))
