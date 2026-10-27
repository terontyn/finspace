import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from argon2.low_level import Type

from app.core.config import settings
from app.core.errors import ApiError

password_hasher = PasswordHasher(type=Type.ID)


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    if password_hash is None:
        return False
    try:
        return password_hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


def create_access_token(user_id: uuid.UUID, *, expires_delta: timedelta | None = None) -> str:
    now = datetime.now(UTC)
    expires = now + (expires_delta or timedelta(minutes=settings.access_token_ttl_minutes))
    payload: dict[str, Any] = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": expires,
        "jti": str(uuid.uuid4()),
    }
    return jwt.encode(payload, settings.jwt_secret_value, algorithm="HS256")


def decode_access_token(token: str) -> uuid.UUID:
    try:
        payload = jwt.decode(token, settings.jwt_secret_value, algorithms=["HS256"])
        if payload.get("type") != "access":
            raise jwt.InvalidTokenError("wrong token type")
        return uuid.UUID(str(payload["sub"]))
    except jwt.ExpiredSignatureError as exc:
        raise ApiError(
            status_code=401,
            code="SESSION_EXPIRED",
            message="Access token has expired",
        ) from exc
    except (jwt.InvalidTokenError, KeyError, ValueError) as exc:
        raise ApiError(
            status_code=401,
            code="INVALID_CREDENTIALS",
            message="Authentication credentials are invalid",
        ) from exc


def new_refresh_secret() -> str:
    return secrets.token_urlsafe(48)


def hash_refresh_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def hash_client_value(value: str | None) -> str | None:
    if not value:
        return None
    return hmac.new(
        settings.jwt_secret_value.encode("utf-8"),
        value.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def constant_hash_match(value: str, expected_hash: str) -> bool:
    return hmac.compare_digest(hash_refresh_secret(value), expected_hash)
