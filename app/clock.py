"""Dates in the board's own time zone (servers run in UTC; the region doesn't)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")


def local_today() -> date:
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ).date()


def local_date(moment: datetime) -> date:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(LOCAL_TZ).date()
