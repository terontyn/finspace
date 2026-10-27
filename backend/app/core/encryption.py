import base64
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings
from app.core.errors import ApiError

MAGIC = b"FSG1"
NONCE_SIZE = 12


def _key() -> bytes:
    encoded = settings.google_token_encryption_key_value
    if not encoded:
        raise ApiError(
            status_code=503,
            code="GOOGLE_NOT_CONFIGURED",
            message="Google token encryption is not configured",
        )
    try:
        key = base64.urlsafe_b64decode(encoded)
    except (ValueError, TypeError) as exc:
        raise ApiError(
            status_code=503,
            code="GOOGLE_NOT_CONFIGURED",
            message="Google token encryption key is invalid",
        ) from exc
    if len(key) != 32:
        raise ApiError(
            status_code=503,
            code="GOOGLE_NOT_CONFIGURED",
            message="Google token encryption key must contain 32 bytes",
        )
    return key


def encrypt_google_secret(value: str, *, purpose: str) -> bytes:
    version = settings.google_token_encryption_key_version
    nonce = os.urandom(NONCE_SIZE)
    aad = f"finspace:{purpose}:v{version}".encode()
    ciphertext = AESGCM(_key()).encrypt(nonce, value.encode(), aad)
    return MAGIC + struct.pack(">I", version) + nonce + ciphertext


def decrypt_google_secret(value: bytes, *, purpose: str) -> str:
    if len(value) < len(MAGIC) + 4 + NONCE_SIZE + 16 or not value.startswith(MAGIC):
        raise ApiError(
            status_code=503,
            code="GOOGLE_TOKEN_EXPIRED",
            message="Encrypted Google credential is invalid",
        )
    offset = len(MAGIC)
    version = struct.unpack(">I", value[offset : offset + 4])[0]
    if version != settings.google_token_encryption_key_version:
        raise ApiError(
            status_code=503,
            code="GOOGLE_NOT_CONFIGURED",
            message="The configured Google encryption key version cannot decrypt this credential",
        )
    nonce = value[offset + 4 : offset + 4 + NONCE_SIZE]
    ciphertext = value[offset + 4 + NONCE_SIZE :]
    aad = f"finspace:{purpose}:v{version}".encode()
    try:
        plaintext = AESGCM(_key()).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise ApiError(
            status_code=503,
            code="GOOGLE_TOKEN_EXPIRED",
            message="Encrypted Google credential cannot be decrypted",
        ) from exc
    try:
        return plaintext.decode()
    except UnicodeDecodeError as exc:
        raise ApiError(
            status_code=503,
            code="GOOGLE_TOKEN_EXPIRED",
            message="Encrypted Google credential is invalid",
        ) from exc
