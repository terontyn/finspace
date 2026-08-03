import base64
import binascii
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlsplit

from redis import Redis
from redis.exceptions import RedisError

from app.core.config import settings

REQUIRED_SCOPES = {
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
}
PLACEHOLDERS = ("change_me", "changeme", "your_", "example", "placeholder")


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    ok: bool
    message: str


def _is_real_value(value: str | None) -> bool:
    normalized = str(value or "").strip().casefold()
    return bool(normalized) and not any(item in normalized for item in PLACEHOLDERS)


def _safe_https(name: str, value: str | None) -> CheckResult:
    parsed = urlsplit(str(value or ""))
    ok = (
        _is_real_value(value)
        and parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )
    return CheckResult(
        name,
        ok,
        "configured HTTPS URL" if ok else "missing or not a safe HTTPS URL",
    )


def _redirect_check() -> CheckResult:
    parsed = urlsplit(settings.google_redirect_uri)
    ok = (
        parsed.scheme in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.path == "/api/v1/integrations/google/callback"
        and not parsed.query
        and not parsed.fragment
    )
    return CheckResult(
        "GOOGLE_REDIRECT_URI",
        ok,
        "valid callback URI" if ok else "must be an HTTP(S) Google callback URI",
    )


def _key_check() -> CheckResult:
    value = settings.google_token_encryption_key_value
    if not _is_real_value(value):
        return CheckResult("GOOGLE_TOKEN_ENCRYPTION_KEY", False, "missing or placeholder")
    try:
        decoded = base64.b64decode(str(value), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError, TypeError):
        decoded = b""
    return CheckResult(
        "GOOGLE_TOKEN_ENCRYPTION_KEY",
        len(decoded) == 32,
        "32 decoded bytes" if len(decoded) == 32 else "must decode to exactly 32 bytes",
    )


def _callback_check() -> CheckResult:
    try:
        with urllib.request.urlopen(settings.google_redirect_uri, timeout=5) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, TimeoutError):
        return CheckResult("backend callback", False, "callback is not reachable")
    ok = 200 <= status < 500
    return CheckResult(
        "backend callback",
        ok,
        f"reachable (HTTP {status})" if ok else f"unexpected HTTP {status}",
    )


def _redis_check() -> CheckResult:
    client = Redis.from_url(settings.redis_url_value, socket_connect_timeout=3, socket_timeout=3)
    try:
        ok = bool(client.ping())
    except RedisError:
        ok = False
    finally:
        client.close()
    return CheckResult(
        "Redis nonce store",
        ok,
        "reachable" if ok else "unreachable; replay protection cannot work",
    )


def _bridge_checks() -> list[CheckResult]:
    skew = settings.google_sheets_webhook_max_clock_skew_seconds
    return [
        CheckResult(
            "GOOGLE_SYNC_PROVIDER",
            settings.google_sync_provider == "apps_script_bridge",
            f"selected: {settings.google_sync_provider}",
        ),
        CheckResult(
            "APPS_SCRIPT_BRIDGE_ENABLED",
            settings.apps_script_bridge_enabled,
            "enabled" if settings.apps_script_bridge_enabled else "disabled",
        ),
        _safe_https("PUBLIC_BACKEND_URL", settings.public_backend_url),
        CheckResult(
            "HMAC timestamp window",
            60 <= skew <= 900,
            f"{skew} seconds" if 60 <= skew <= 900 else "must be between 60 and 900 seconds",
        ),
        CheckResult(
            "GOOGLE_SHEETS_SYNC_ENABLED",
            settings.google_sheets_sync_enabled,
            "enabled" if settings.google_sheets_sync_enabled else "disabled",
        ),
        _redis_check(),
        CheckResult(
            "Google OAuth credentials",
            True,
            "not required by apps_script_bridge",
        ),
    ]


def _oauth_checks() -> list[CheckResult]:
    return [
        CheckResult(
            "GOOGLE_SYNC_PROVIDER",
            settings.google_sync_provider == "google_oauth",
            f"selected: {settings.google_sync_provider}",
        ),
        CheckResult(
            "GOOGLE_OAUTH_ENABLED",
            settings.google_oauth_enabled,
            "enabled" if settings.google_oauth_enabled else "disabled",
        ),
        CheckResult(
            "GOOGLE_CLIENT_ID",
            _is_real_value(settings.google_client_id_value),
            "configured" if _is_real_value(settings.google_client_id_value) else "missing",
        ),
        CheckResult(
            "GOOGLE_CLIENT_SECRET",
            _is_real_value(settings.google_client_secret_value),
            "configured" if _is_real_value(settings.google_client_secret_value) else "missing",
        ),
        _redirect_check(),
        _key_check(),
        _safe_https("PUBLIC_WEBHOOK_BASE_URL", settings.public_webhook_base_url),
        CheckResult(
            "Google OAuth scopes",
            REQUIRED_SCOPES.issubset(settings.google_scopes),
            "required scopes present"
            if REQUIRED_SCOPES.issubset(settings.google_scopes)
            else "required scopes are missing",
        ),
        _callback_check(),
    ]


def run_checks() -> list[CheckResult]:
    if settings.google_sync_provider == "apps_script_bridge":
        return _bridge_checks()
    return _oauth_checks()


def main() -> None:
    results = run_checks()
    for result in results:
        print(f"{'PASS' if result.ok else 'FAIL'}: {result.name} — {result.message}")
    print("Secret values were not printed")
    if not all(result.ok for result in results):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
