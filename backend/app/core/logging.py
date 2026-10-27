import contextvars
import json
import logging
import logging.config
from datetime import UTC, datetime
from typing import Any

from app.core.config import settings

request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    """Compact JSON logs suitable for local Docker output and later aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": "backend",
            "message": record.getMessage(),
            "request_id": request_id_context.get(),
            "logger": record.name,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    """Install the single JSON console handler on the root logger.

    ``dictConfig`` replaces the root handler list rather than appending to it, so calling this more
    than once — the API process at import, each worker in its ``main()`` — is idempotent and never
    duplicates a line.

    ``LOG_LEVEL=INFO`` applies to the root logger, so third-party loggers inherit it too. ``httpx``
    emits one INFO record per request containing the full request URL, and Google Sheets URLs carry
    the spreadsheet identifier and A1 ranges; that is not information this application needs in
    normal output. Those two loggers are therefore bounded to WARNING instead of being disabled, so
    genuine transport failures still surface. SQLAlchemy needs no equivalent bound: importing it
    pins the ``sqlalchemy`` logger to WARNING, so statements and bound parameters stay unlogged
    unless ``echo`` is enabled, which this project never does.
    """
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": "app.core.logging.JsonFormatter"}},
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                    "stream": "ext://sys.stdout",
                }
            },
            "loggers": {
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
            },
            "root": {"handlers": ["console"], "level": settings.log_level},
        }
    )
