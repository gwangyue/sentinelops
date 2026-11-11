"""占住 N 个 MySQL 连接，直到被 kill（故障脚本 connections_exhausted.sh 在后台启动它）。

一个进程开 N 个连接，每个连接都是空闲会话（模拟连接池泄漏）。第一版在 Pod 里起 N 个 mysql 客户端进程，
容器内存被撑爆、MySQL 被 OOMKill（M4 D13 实测），所以改成从本机通过 port-forward 连。
用法：python hold_connections.py <port> <root 密码> <N>
"""

import signal
import sys
import time

import pymysql

port, password, n = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
conns = []
for _ in range(n):
    try:
        conns.append(pymysql.connect(host="127.0.0.1", port=port, user="root", password=password, connect_timeout=10))
    except pymysql.MySQLError as e:
        print(f"第 {len(conns) + 1} 个连接失败：{e}", file=sys.stderr)
        break
print(f"holding {len(conns)} connections", flush=True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
while True:
    time.sleep(30)
    for c in conns:
        c.ping(reconnect=False)  # 保持连接活着（wait_timeout 很长，这里只是防止中间设备断开）
