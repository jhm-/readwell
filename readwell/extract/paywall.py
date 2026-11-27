"""Paywall detection and archive links.

Readwell reads what the publisher serves. When a page is paywalled it links to an
*existing* archive.today snapshot (``/newest/``); it never submits pages to archive
services or scrapes them, and never disguises itself to get past a paywall.

* ``hard`` — the text isn't in the page (short extraction plus paywall signals)
* ``soft`` — paywall markup is present but the full text was in the page anyway
* ``none``
"""

from __future__ import annotations

import re
from urllib.parse import quote

FREE_FALSE = re.compile(r'"isAccessibleForFree"\s*:\s*"?(false|False|FALSE)"?')
MARKUP = re.compile(
    r"""(class|id)\s*=\s*["'][^"']*\b(paywall|pay-wall|piano|tp-modal|tp-container|regwall|meter(ed)?-?(wall|banner)?|"""
    r"""subscriber-only|subscribers-only|premium-content|article-locked|locked-content|zephr)\b""",
    re.I,
)
PHRASES = re.compile(
    r"(subscribe (now )?to (continue|keep) reading|to continue reading,? (please )?(subscribe|sign in|log in)|"
    r"this (article|story|content) is (only )?(available )?(for|to) (paid )?subscribers|already a subscriber\?|"
    r"subscribers only|become a (paid )?subscriber to read|you('ve| have) reached your (free )?(article )?limit)",
    re.I,
)
HARD_MAX_WORDS = 150


def detect(html: str, extracted_words: int) -> str:
    signals = 0
    if FREE_FALSE.search(html):
        signals += 2
    if MARKUP.search(html):
        signals += 1
    if PHRASES.search(html):
        signals += 1
    if signals == 0:
        return "none"
    return "hard" if extracted_words < HARD_MAX_WORDS else "soft"


def archive_url(url: str) -> str:
    return "https://archive.ph/newest/" + quote(url, safe=":/?&=%#+")
