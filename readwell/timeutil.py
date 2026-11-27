"""UTC timestamps stored as sortable ISO-8601 strings (``2026-11-26T20:15:00Z``)."""

from __future__ import annotations

import calendar
import time
from datetime import UTC, datetime, timedelta

FMT = "%Y-%m-%dT%H:%M:%SZ"


def now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime(FMT)


def now_iso() -> str:
    return to_iso(now())


def from_iso(s: str) -> datetime:
    return datetime.strptime(s, FMT).replace(tzinfo=UTC)


def iso_ago(**kwargs: float) -> str:
    return to_iso(now() - timedelta(**kwargs))


def iso_in(**kwargs: float) -> str:
    return to_iso(now() + timedelta(**kwargs))


def struct_to_iso(st: time.struct_time | None) -> str | None:
    """Convert a feedparser ``*_parsed`` struct (always UTC) to ISO, rejecting junk.

    Dates more than a day in the future are treated as missing: some feeds stamp
    items with bogus far-future dates, which would pin them to the top forever.
    """
    if not st:
        return None
    try:
        dt = datetime.fromtimestamp(calendar.timegm(st), UTC)
    except (OverflowError, ValueError, OSError):
        return None
    if dt > now() + timedelta(days=1) or dt.year < 1995:
        return None
    return to_iso(dt)
