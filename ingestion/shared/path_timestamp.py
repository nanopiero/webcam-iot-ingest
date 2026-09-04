"""Best-effort extraction of a UTC timestamp from a provider object path."""

from __future__ import annotations

from datetime import UTC, datetime
import re
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_DATE = re.compile(
    r"(?<!\d)(?P<year>20\d{2})[/_-]?(?P<month>0[1-9]|1[0-2])"
    r"[/_-]?(?P<day>0[1-9]|[12]\d|3[01])(?!\d)"
)
_TIME = re.compile(
    r"(?<!\d)(?P<hour>[01]\d|2[0-3])[-_:]?"
    r"(?P<minute>[0-5]\d)(?:[-_:]?(?P<second>[0-5]\d))?(?!\d)"
)


def timestamp_from_path(
    path_or_url: str | None, timezone_name: str | None
) -> datetime | None:
    """Return a path-encoded local timestamp as UTC, or None if ambiguous."""
    if not path_or_url or not timezone_name:
        return None
    path = unquote(urlsplit(path_or_url).path)
    date_matches = list(_DATE.finditer(path))
    filename = path.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    time_matches = list(_TIME.finditer(filename))
    if len(date_matches) != 1 or len(time_matches) != 1:
        return None
    date_match = date_matches[0]
    time_match = time_matches[0]
    try:
        timezone = ZoneInfo(timezone_name)
        local = datetime(
            int(date_match.group("year")),
            int(date_match.group("month")),
            int(date_match.group("day")),
            int(time_match.group("hour")),
            int(time_match.group("minute")),
            int(time_match.group("second") or 0),
            tzinfo=timezone,
        )
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return local.astimezone(UTC)
