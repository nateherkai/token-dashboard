"""Timezone conversion without changing stored UTC timestamps."""
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones


@lru_cache(maxsize=1)
def timezone_names():
    return frozenset(name for name in available_timezones()
                     if '/' in name and not name.startswith(('posix/', 'right/'))) | {'UTC'}


def zone_for(name):
    if not isinstance(name, str) or name not in timezone_names():
        raise ValueError('Choose a valid IANA timezone, such as America/Los_Angeles')
    if name == 'UTC':
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError('Choose a valid IANA timezone, such as America/Los_Angeles') from exc


def local_day(timestamp, zone):
    try:
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        return dt.replace(tzinfo=dt.tzinfo or timezone.utc).astimezone(zone).date().isoformat()
    except (ValueError, TypeError, AttributeError):
        return None


def day_range(days, timezone_name, now=None):
    """UTC bounds for calendar days including today; DST days may be 23/25 hours."""
    zone = zone_for(timezone_name)
    today = (now or datetime.now(timezone.utc)).astimezone(zone).date()
    start = datetime.combine(today - timedelta(days=days - 1), time.min, zone)
    end = datetime.combine(today + timedelta(days=1), time.min, zone)
    return start.astimezone(timezone.utc).isoformat(), end.astimezone(timezone.utc).isoformat()
