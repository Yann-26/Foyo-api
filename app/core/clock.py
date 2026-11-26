"""The households' clock. Every "now" / "today" goes through here, never the server's local time."""

from datetime import date, datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from app.core.config import get_settings


@lru_cache
def _zone() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def now() -> datetime:
    """Wall-clock time in the households' timezone, naive (as stored in the database)."""
    return datetime.now(_zone()).replace(tzinfo=None)


def today() -> date:
    return now().date()


def to_utc(local: datetime) -> datetime:
    """A naive households-time datetime → aware UTC (what a phone needs to schedule an alarm)."""
    from datetime import UTC

    return local.replace(tzinfo=_zone()).astimezone(UTC)


def to_local(moment: datetime) -> datetime:
    """A time sent by a phone (aware, any zone; or naive = already households time) → naive households time."""
    if moment.tzinfo is None:
        return moment
    return moment.astimezone(_zone()).replace(tzinfo=None)
