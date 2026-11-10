from fastapi.testclient import TestClient

from sentinel.api.app import app


def test_healthz() -> None:
    # TestClient 走进程内 ASGI 调用，不开 socket
    assert TestClient(app).get("/healthz").json() == {"status": "ok"}
