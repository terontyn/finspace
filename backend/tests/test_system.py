import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.routes import system as system_routes


def test_system_info_does_not_expose_secrets(client: TestClient, monkeypatch: object) -> None:
    monkeypatch.setattr(  # type: ignore[attr-defined]
        system_routes, "get_schema_version", AsyncMock(return_value="0001_foundation")
    )

    response = client.get("/api/v1/system/info")
    payload = response.json()
    serialized = json.dumps(payload).lower()

    assert response.status_code == 200
    assert payload["database_schema_version"] == "0001_foundation"
    assert "password" not in serialized
    assert "database_url" not in serialized
    assert "redis_url" not in serialized
    assert "test_password" not in serialized
