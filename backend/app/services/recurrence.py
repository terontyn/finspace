from calendar import monthrange
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import ApiError

WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
SUPPORTED_PARTS = {
    "FREQ",
    "INTERVAL",
    "BYDAY",
    "BYMONTHDAY",
    "BYMONTH",
    "BYHOUR",
    "BYMINUTE",
    "UNTIL",
}


def timezone(value: str) -> ZoneInfo:
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ApiError(
            status_code=422,
            code="RECURRING_RULE_INVALID",
            message="Recurring rule timezone is invalid",
        ) from exc


def parse_rrule(value: str) -> dict[str, str]:
    raw = value.strip().upper()
    if raw.startswith("RRULE:"):
        raw = raw[6:]
    parts: dict[str, str] = {}
    for item in raw.split(";"):
        key, separator, part_value = item.partition("=")
        if not separator or not key or not part_value or key in parts:
            raise _invalid_rrule()
        if key not in SUPPORTED_PARTS:
            raise _invalid_rrule(f"Unsupported RRULE part: {key}")
        parts[key] = part_value
    if parts.get("FREQ") not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
        raise _invalid_rrule("FREQ must be DAILY, WEEKLY, MONTHLY or YEARLY")
    _positive_integer(parts, "INTERVAL", default="1", upper=366)
    _integer_list(parts, "BYHOUR", 0, 23)
    _integer_list(parts, "BYMINUTE", 0, 59)
    _integer_list(parts, "BYMONTH", 1, 12)
    month_days = _integer_list(parts, "BYMONTHDAY", -31, 31)
    if 0 in month_days:
        raise _invalid_rrule("BYMONTHDAY cannot contain zero")
    if "BYDAY" in parts:
        days = parts["BYDAY"].split(",")
        if not days or any(day not in WEEKDAYS for day in days):
            raise _invalid_rrule("BYDAY contains an invalid weekday")
    if "UNTIL" in parts:
        _parse_until(parts["UNTIL"])
    return parts


def normalize_rrule(value: str) -> str:
    parts = parse_rrule(value)
    return ";".join(f"{key}={part_value}" for key, part_value in parts.items())


def next_occurrence(
    value: str,
    timezone_name: str,
    *,
    after: datetime,
    anchor: datetime | None = None,
) -> datetime | None:
    parts = parse_rrule(value)
    zone = timezone(timezone_name)
    after_utc = after.astimezone(UTC)
    local_after = after_utc.astimezone(zone)
    local_anchor = (anchor or after).astimezone(zone)
    hours = _integer_list(parts, "BYHOUR", 0, 23) or [local_anchor.hour]
    minutes = _integer_list(parts, "BYMINUTE", 0, 59) or [local_anchor.minute]
    until = _parse_until(parts["UNTIL"]) if "UNTIL" in parts else None
    for day_offset in range(0, 366 * 20):
        candidate_date = local_after.date() + timedelta(days=day_offset)
        if not _date_matches(candidate_date, local_anchor.date(), parts):
            continue
        for hour in hours:
            for minute in minutes:
                candidate = datetime.combine(
                    candidate_date, time(hour=hour, minute=minute), tzinfo=zone
                ).astimezone(UTC)
                if candidate <= after_utc:
                    continue
                if until is not None and candidate > until:
                    return None
                return candidate
    raise _invalid_rrule("RRULE does not produce an occurrence within 20 years")


def _date_matches(candidate: date, anchor: date, parts: dict[str, str]) -> bool:
    interval = int(parts.get("INTERVAL", "1"))
    freq = parts["FREQ"]
    by_days = {WEEKDAYS[item] for item in parts["BYDAY"].split(",")} if "BYDAY" in parts else None
    by_months = set(_integer_list(parts, "BYMONTH", 1, 12))
    by_month_days = set(_resolved_month_days(candidate, parts))
    if by_days is not None and candidate.weekday() not in by_days:
        return False
    if by_months and candidate.month not in by_months:
        return False
    if by_month_days and candidate.day not in by_month_days:
        return False
    if freq == "DAILY":
        return (candidate - anchor).days >= 0 and (candidate - anchor).days % interval == 0
    if freq == "WEEKLY":
        days = (candidate - anchor).days
        return (
            days >= 0
            and days // 7 % interval == 0
            and (by_days is not None or candidate.weekday() == anchor.weekday())
        )
    if freq == "MONTHLY":
        months = (candidate.year - anchor.year) * 12 + candidate.month - anchor.month
        return (
            months >= 0
            and months % interval == 0
            and (bool(by_month_days) or candidate.day == anchor.day)
        )
    years = candidate.year - anchor.year
    return (
        years >= 0
        and years % interval == 0
        and (bool(by_months) or candidate.month == anchor.month)
        and (bool(by_month_days) or candidate.day == anchor.day)
    )


def _resolved_month_days(candidate: date, parts: dict[str, str]) -> list[int]:
    values = _integer_list(parts, "BYMONTHDAY", -31, 31)
    last_day = monthrange(candidate.year, candidate.month)[1]
    return [value if value > 0 else last_day + value + 1 for value in values]


def _integer_list(parts: dict[str, str], key: str, lower: int, upper: int) -> list[int]:
    if key not in parts:
        return []
    try:
        values = [int(item) for item in parts[key].split(",")]
    except ValueError as exc:
        raise _invalid_rrule(f"{key} must contain integers") from exc
    if not values or any(value < lower or value > upper for value in values):
        raise _invalid_rrule(f"{key} is outside the supported range")
    return sorted(set(values))


def _positive_integer(parts: dict[str, str], key: str, *, default: str, upper: int) -> int:
    try:
        value = int(parts.get(key, default))
    except ValueError as exc:
        raise _invalid_rrule(f"{key} must be an integer") from exc
    if value < 1 or value > upper:
        raise _invalid_rrule(f"{key} is outside the supported range")
    return value


def _parse_until(value: str) -> datetime:
    try:
        if len(value) == 8:
            return datetime.strptime(value, "%Y%m%d").replace(
                hour=23, minute=59, second=59, tzinfo=UTC
            )
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise _invalid_rrule("UNTIL must use YYYYMMDD or UTC YYYYMMDDTHHMMSSZ") from exc


def _invalid_rrule(message: str = "RRULE is invalid") -> ApiError:
    return ApiError(status_code=422, code="RECURRING_RULE_INVALID", message=message)
