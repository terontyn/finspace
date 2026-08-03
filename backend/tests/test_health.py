from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.routes import health as health_routes


def test_health_returns_expected_payload(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "backend"}


def test_request_id_is_generated_and_echoed(client: TestClient) -> None:
    generated = client.get("/api/v1/health").headers["x-request-id"]
    supplied = client.get("/api/v1/health", headers={"X-Request-ID": "test-request-id"})

    assert generated
    assert supplied.headers["x-request-id"] == "test-request-id"


def test_dependency_error_uses_common_format(client: TestClient, monkeypatch: object) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        health_routes, "check_database", AsyncMock(side_effect=ConnectionError())
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        health_routes, "check_redis", AsyncMock(return_value=None)
    )

    response = client.get("/api/v1/health/ready")
    body = response.json()

    assert response.status_code == 503
    assert body["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
    assert body["error"]["details"]["checks"] == {
        "database": "unavailable",
        "redis": "ok",
    }
