import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.core.security import create_access_token
from app.db.models.users import User, WorkspaceMember
from app.db.session import AsyncSessionFactory

PASSWORD = "correct horse battery staple"


def _registration(email: str | None = None) -> dict[str, str]:
    return {
        "email": email or f"auth-{uuid.uuid4()}@example.com",
        "display_name": "Auth Test",
        "password": PASSWORD,
        "workspace_name": "Auth Workspace",
        "base_currency": "RUB",
        "timezone": "Europe/Amsterdam",
    }


def _register(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, email: str | None = None
) -> dict[str, object]:
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(settings, "allow_registration", True)
    response = client.post("/api/v1/auth/register", json=_registration(email))
    assert response.status_code == 201, response.text
    assert settings.auth_cookie_name in response.cookies
    return response.json()


def test_registration_creates_workspace_member_and_argon2_hash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = f"register-{uuid.uuid4()}@example.com"
    result = _register(client, monkeypatch, email)

    async def inspect() -> tuple[str | None, str | None]:
        async with AsyncSessionFactory() as session:
            user = await session.scalar(
                select(User).where(User.id == uuid.UUID(result["user"]["id"]))
            )
            member = await session.scalar(
                select(WorkspaceMember.role).where(
                    WorkspaceMember.user_id == uuid.UUID(result["user"]["id"]),
                    WorkspaceMember.workspace_id == uuid.UUID(result["workspace"]["id"]),
                )
            )
            return user.password_hash if user else None, member

    password_hash, role = asyncio.run(inspect())
    assert password_hash is not None and password_hash.startswith("$argon2id$")
    assert PASSWORD not in password_hash
    assert role == "owner"

    duplicate = client.post("/api/v1/auth/register", json=_registration(email.upper()))
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "DUPLICATE_EMAIL"


def test_login_access_and_expired_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = f"login-{uuid.uuid4()}@example.com"
    registered = _register(client, monkeypatch, email)
    wrong = client.post("/api/v1/auth/login", json={"email": email, "password": "wrong"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "INVALID_CREDENTIALS"

    login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert login.status_code == 200
    access = login.json()["access_token"]
    protected = client.get("/api/v1/accounts", headers={"Authorization": f"Bearer {access}"})
    assert protected.status_code == 200

    expired = create_access_token(
        uuid.UUID(registered["user"]["id"]), expires_delta=timedelta(seconds=-1)
    )
    denied = client.get("/api/v1/accounts", headers={"Authorization": f"Bearer {expired}"})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "SESSION_EXPIRED"


def test_refresh_rotation_reuse_logout_and_logout_all(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = f"refresh-{uuid.uuid4()}@example.com"
    _register(client, monkeypatch, email)
    login = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    old_cookie = login.cookies[settings.auth_cookie_name]

    rotated = client.post("/api/v1/auth/refresh")
    assert rotated.status_code == 200
    new_cookie = rotated.cookies[settings.auth_cookie_name]
    assert new_cookie != old_cookie

    client.cookies.clear()
    client.cookies.set(settings.auth_cookie_name, old_cookie, path="/api/v1/auth")
    reused = client.post("/api/v1/auth/refresh")
    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "TOKEN_REUSE_DETECTED"

    login_again = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    logout_cookie = login_again.cookies[settings.auth_cookie_name]
    assert client.post("/api/v1/auth/logout").status_code == 200
    client.cookies.clear()
    client.cookies.set(settings.auth_cookie_name, logout_cookie, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").json()["error"]["code"] == "SESSION_REVOKED"

    first = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    first_cookie = first.cookies[settings.auth_cookie_name]
    second = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    access = second.json()["access_token"]
    all_logout = client.post(
        "/api/v1/auth/logout-all", headers={"Authorization": f"Bearer {access}"}
    )
    assert all_logout.status_code == 200
    client.cookies.clear()
    client.cookies.set(settings.auth_cookie_name, first_cookie, path="/api/v1/auth")
    assert client.post("/api/v1/auth/refresh").json()["error"]["code"] == "SESSION_REVOKED"


def test_roles_and_dev_headers_default_off(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _register(client, monkeypatch, f"owner-{uuid.uuid4()}@example.com")
    viewer = _register(client, monkeypatch, f"viewer-{uuid.uuid4()}@example.com")
    editor = _register(client, monkeypatch, f"editor-{uuid.uuid4()}@example.com")
    workspace_id = uuid.UUID(owner["workspace"]["id"])

    async def add_roles() -> None:
        async with AsyncSessionFactory() as session:
            session.add_all(
                [
                    WorkspaceMember(
                        workspace_id=workspace_id,
                        user_id=uuid.UUID(viewer["user"]["id"]),
                        role="viewer",
                    ),
                    WorkspaceMember(
                        workspace_id=workspace_id,
                        user_id=uuid.UUID(editor["user"]["id"]),
                        role="editor",
                    ),
                ]
            )
            await session.commit()

    asyncio.run(add_roles())
    workspace_header = {"X-Workspace-ID": str(workspace_id)}
    viewer_headers = {
        **workspace_header,
        "Authorization": f"Bearer {viewer['access_token']}",
    }
    editor_headers = {
        **workspace_header,
        "Authorization": f"Bearer {editor['access_token']}",
    }
    payload = {
        "name": f"Role account {uuid.uuid4()}",
        "account_type": "cash",
        "currency": "RUB",
        "opening_balance": "0.0000",
        "opening_balance_at": "2026-07-22T00:00:00Z",
    }
    assert client.get("/api/v1/accounts", headers=viewer_headers).status_code == 200
    denied = client.post("/api/v1/accounts", headers=viewer_headers, json=payload)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert client.post("/api/v1/accounts", headers=editor_headers, json=payload).status_code == 201

    owner_headers = {"Authorization": f"Bearer {owner['access_token']}"}
    members = client.get("/api/v1/workspaces/current/members", headers=owner_headers)
    assert members.status_code == 200
    removed = client.delete(
        f"/api/v1/workspaces/current/members/{viewer['user']['id']}",
        headers=owner_headers,
    )
    assert removed.status_code == 204

    monkeypatch.setattr(settings, "allow_dev_auth_headers", False)
    dev_headers = {
        "X-User-ID": owner["user"]["id"],
        "X-Workspace-ID": owner["workspace"]["id"],
    }
    assert client.get("/api/v1/accounts", headers=dev_headers).status_code == 401
