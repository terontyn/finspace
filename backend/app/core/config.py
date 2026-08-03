from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-only application configuration."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
    )

    project_name: str = "Finspace"
    environment: Literal["development", "test", "production"] = "development"
    testing: bool = False
    debug: bool = False
    backend_version: str = "0.1.0"

    database_url: SecretStr
    redis_url: SecretStr
    jwt_secret_key: SecretStr = SecretStr("change_me_with_long_random_value")
    access_token_ttl_minutes: int = Field(default=15, ge=1, le=120)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)
    auth_cookie_name: str = "finspace_refresh"
    auth_cookie_secure: bool = False
    auth_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    allow_registration: bool = True
    allow_dev_auth_headers: bool = False

    import_max_file_size_mb: int = Field(default=20, ge=1, le=200)
    import_max_rows: int = Field(default=100000, ge=1, le=1000000)
    import_allowed_extensions: str = "csv,xlsx"
    import_storage_path: Path = Path("/app/data/imports")

    google_client_id: SecretStr | None = None
    google_client_secret: SecretStr | None = None
    google_sync_provider: Literal["apps_script_bridge", "google_oauth"] = "apps_script_bridge"
    google_oauth_enabled: bool = False
    apps_script_bridge_enabled: bool = True
    public_backend_url: str | None = None
    apps_script_pull_batch_size: int = Field(default=100, ge=1, le=500)
    apps_script_heartbeat_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    google_redirect_uri: str = "http://localhost:8000/api/v1/integrations/google/callback"
    google_oauth_scopes: str = (
        "openid,email,profile,https://www.googleapis.com/auth/spreadsheets,"
        "https://www.googleapis.com/auth/drive.file"
    )
    google_token_encryption_key: SecretStr | None = None
    google_token_encryption_key_version: int = Field(default=1, ge=1)
    google_sheets_sync_enabled: bool = True
    google_sheets_worker_batch_size: int = Field(default=100, ge=1, le=500)
    google_sheets_worker_poll_seconds: int = Field(default=5, ge=1, le=300)
    google_sheets_max_retry_attempts: int = Field(default=8, ge=1, le=20)
    google_sheets_webhook_max_clock_skew_seconds: int = Field(default=300, ge=30, le=3600)
    google_sheets_template_version: int = Field(default=1, ge=1)
    public_webhook_base_url: str | None = None

    n8n_heartbeat_stale_minutes: int = Field(default=15, ge=1, le=1440)
    backup_stale_hours: int = Field(default=36, ge=1, le=8760)
    backup_metadata_path: Path = Path("/app/backups/database")
    backup_remote_provider: Literal["disabled", "local_secondary_path"] = "disabled"
    backup_secondary_path: Path | None = None
    backup_remote_after_verify: bool = True

    backend_host: str = "0.0.0.0"
    backend_port: int = Field(default=8000, ge=1, le=65535)
    cors_origins: str = "http://localhost:3000"
    log_level: Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"] = "INFO"

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, value: str) -> str:
        origins = [origin.strip() for origin in value.split(",") if origin.strip()]
        if not origins:
            raise ValueError("CORS_ORIGINS must contain at least one origin")
        if "*" in origins:
            raise ValueError("CORS_ORIGINS must not contain a wildcard")
        return ",".join(origins)

    @model_validator(mode="after")
    def validate_security(self) -> "Settings":
        secret = self.jwt_secret_key.get_secret_value()
        unsafe = secret.startswith("change_me") or len(secret) < 32
        if self.environment == "production" and unsafe:
            raise ValueError("JWT_SECRET_KEY must be at least 32 non-placeholder characters")
        if self.environment == "production" and self.allow_dev_auth_headers:
            raise ValueError("ALLOW_DEV_AUTH_HEADERS is forbidden in production")
        if self.environment == "production" and self.testing:
            raise ValueError("TESTING=true is forbidden in production")
        if self.environment == "test" and not self.testing:
            raise ValueError("TESTING=true is required when ENVIRONMENT=test")
        if self.environment == "production" and not self.auth_cookie_secure:
            self.auth_cookie_secure = True
        return self

    @property
    def database_url_value(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def redis_url_value(self) -> str:
        return self.redis_url.get_secret_value()

    @property
    def jwt_secret_value(self) -> str:
        return self.jwt_secret_key.get_secret_value()

    @staticmethod
    def _secret_value(value: SecretStr | None) -> str | None:
        if value is None:
            return None
        unwrapped = value.get_secret_value().strip()
        return unwrapped or None

    @property
    def google_client_id_value(self) -> str | None:
        return self._secret_value(self.google_client_id)

    @property
    def google_client_secret_value(self) -> str | None:
        return self._secret_value(self.google_client_secret)

    @property
    def google_token_encryption_key_value(self) -> str | None:
        return self._secret_value(self.google_token_encryption_key)

    @property
    def google_scopes(self) -> list[str]:
        return [scope.strip() for scope in self.google_oauth_scopes.split(",") if scope.strip()]

    @property
    def google_is_configured(self) -> bool:
        return all(
            (
                self.google_oauth_enabled,
                self.google_sheets_sync_enabled,
                self.google_client_id_value,
                self.google_client_secret_value,
                self.google_token_encryption_key_value,
                self.google_redirect_uri,
            )
        )

    @property
    def apps_script_bridge_is_configured(self) -> bool:
        return all(
            (
                self.google_sync_provider == "apps_script_bridge",
                self.google_sheets_sync_enabled,
                self.apps_script_bridge_enabled,
                self.public_backend_url,
            )
        )

    @property
    def google_provider_is_configured(self) -> bool:
        if self.google_sync_provider == "apps_script_bridge":
            return self.apps_script_bridge_is_configured
        return self.google_is_configured

    @property
    def allowed_import_extensions(self) -> set[str]:
        return {value.strip().lower() for value in self.import_allowed_extensions.split(",")}

    @property
    def allowed_cors_origins(self) -> list[str]:
        return self.cors_origins.split(",")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
