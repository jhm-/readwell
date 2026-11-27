"""Cheap keyword scoring that decides whether an item is worth an LLM call, plus source mutes.

A topic's score is ``min(1, (3 × title hits + body hits) / 10)``, counting each keyword once.
One keyword in the title (0.3) or two in the body (0.2) clears the default floor of 0.15.

Matching is case-insensitive on word boundaries, except short all-capital keywords
(``CN``, ``MTO``, ``IBA``), which must match in capitals so "cn" in a URL slug doesn't count.
"""

from __future__ import annotations

import re
from functools import lru_cache

from .topics import Topic

BODY_WORDS = 1500


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern[str]:
    if len(term) > 2 and term.startswith("/") and term.endswith("/"):
        return re.compile(term[1:-1], re.I)
    flags = 0 if (term.isupper() and len(term) <= 5) else re.I
    return re.compile(r"(?<![\w-])" + re.escape(term).replace(r"\ ", r"[\s-]+") + r"(?![\w-])", flags)


def matches(term: str, text: str) -> bool:
    try:
        return bool(_pattern(term).search(text))
    except re.error:
        return False


def muted_by(terms: list[str], title: str, text: str) -> str | None:
    hay = f"{title}\n{text}"
    for t in terms:
        if matches(t, hay):
            return t
    return None


def _body(text: str) -> str:
    words = text.split()
    return " ".join(words[:BODY_WORDS])


def score(topics: list[Topic], title: str, text: str, extra: dict[str, list[str]] | None = None) -> dict[str, float]:
    """Keyword score 0-1 per topic (only topics with at least one hit)."""
    body = _body(text)
    out: dict[str, float] = {}
    for t in topics:
        terms = list(dict.fromkeys([*t.keywords, *(extra or {}).get(t.id, [])]))
        if not terms:
            continue
        in_title = sum(1 for k in terms if matches(k, title))
        in_body = sum(1 for k in terms if matches(k, body))
        s = min(1.0, (3 * in_title + in_body) / 10)
        if s > 0:
            out[t.id] = round(s, 3)
    return out
