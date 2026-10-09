"""健康检查接口（见《项目介绍.md》8.2、10.6）。"""

from fastapi.testclient import TestClient

from app.core.selfcheck import SelfCheckError


def test_live_returns_ok(client: TestClient) -> None:
    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_reports_storage_checks(client: TestClient) -> None:
    response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"sqlite": "ok", "fts5": "ok", "chroma": "ok"},
    }


def test_ready_returns_503_when_selfcheck_fails(
    client: TestClient, monkeypatch
) -> None:
    def fail(_settings: object) -> None:
        raise SelfCheckError("存储不可用")

    monkeypatch.setattr("app.api.health.run_startup_checks", fail)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "error", "detail": "存储不可用"}
