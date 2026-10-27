import hashlib
import json
import re
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any


def canonical_decimal(value: Decimal | str | int) -> str:
    decimal = Decimal(str(value))
    normalized = format(decimal.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def canonical_text(value: str | None) -> str | None:
    return re.sub(r"\s+", " ", value.strip()) if value is not None else None


def canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return canonical_text(value)
    return value


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        canonical_value(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def row_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
