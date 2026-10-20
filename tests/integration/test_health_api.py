"""健康检查接口（见《项目开发说明_v1.md》8.2、10.6）。"""

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


def test_ready_returns_503_without_leaking_exception(
    client: TestClient, monkeypatch
) -> None:
    def fail(_db_path: object) -> None:
        raise SelfCheckError("目录不可写: /secret/path")

    monkeypatch.setattr("app.api.health.check_fts5_available", fail)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "error",
        "checks": {"sqlite": "ok", "fts5": "error", "chroma": "ok"},
    }
    assert "secret" not in response.text


def test_ready_reports_unavailable_chroma(
    client: TestClient, monkeypatch
) -> None:
    def boom() -> int:
        raise RuntimeError("collection missing")

    container = client.app.state.container
    monkeypatch.setattr(container.vector_index, "count", boom)

    response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["chroma"] == "error"
    assert "collection missing" not in response.text
