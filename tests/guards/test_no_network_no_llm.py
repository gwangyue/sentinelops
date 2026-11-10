"""确认"tests/ 不访问网络、不调用真实 LLM"这两道闸本身在生效。"""

import socket

import pytest
from pytest_socket import SocketBlockedError

from sentinel.core.llm.config import RealModelInTestError, guard_real_call


def test_network_blocked() -> None:
    with pytest.raises(SocketBlockedError):
        socket.create_connection(("example.com", 443), timeout=1)


def test_real_model_blocked() -> None:
    with pytest.raises(RealModelInTestError):
        guard_real_call("diagnosis")
