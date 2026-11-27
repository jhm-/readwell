"""UTC timestamps stored as sortable ISO-8601 strings (``2026-11-26T20:15:00Z``)."""

from __future__ import annotations

import calendar
import re
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


def parse_human_date(s: str | None) -> str | None:
    """Dates as people write them on web pages and in documents ("March 9, 2026", "Nov 26, 2026",
    "13th January 2026", RFC 822, ISO 8601). Returns ISO UTC (noon for date-only values) or None."""
    if not s:
        return None
    from feedparser.datetimes import _parse_date  # RFC 822, ISO 8601, W3C-DTF and more

    s = re.sub(r"\s+", " ", s.replace(" ,", ",")).strip()
    st = _parse_date(s)
    if st:
        return struct_to_iso(st)
    s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", s)
    for fmt in ("%B %d, %Y", "%B %d %Y", "%d %B %Y", "%d %B, %Y", "%Y-%m-%d", "%b %d, %Y", "%b. %d, %Y", "%b %d %Y"):
        try:
            return to_iso(datetime.strptime(s, fmt).replace(hour=12, tzinfo=UTC))
        except ValueError:
            continue
    return None
