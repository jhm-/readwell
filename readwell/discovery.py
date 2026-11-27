"""Find the RSS/Atom/JSON feed behind a page URL.

Order of attempts:
1. the URL itself is a feed;
2. ``<link rel="alternate">`` feed links in the page (in document order);
3. ``<a href>`` links that look like feeds (``.rss``, ``/feed``, ``/rss`` …);
4. common feed paths, first under the page's own path, then at the site root.

Candidates are fetched and accepted only if feedparser recognizes a feed.
Comment feeds are pushed to the back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
from selectolax.lexbor import LexborHTMLParser as HTMLParser

from .fetch.http import Fetcher, Response
from .models import DiscoveryError, NetworkError, SourceError

FEED_TYPES = {
    "application/rss+xml",
    "application/atom+xml",
    "application/feed+json",
    "application/rdf+xml",
    "application/xml",
    "text/xml",
    "application/json",
}
COMMON_PATHS = ["feed", "rss", "rss.xml", "feed.xml", "atom.xml", "index.xml", "feeds/posts/default", "?feed=rss2"]
ANCHOR_HINT = re.compile(r"(\.rss|\.atom|/rss(/|$|\.)|/feeds?(/|$)|rss\.xml|atom\.xml|feed\.xml|format=rss)", re.I)
MAX_CANDIDATES = 12


@dataclass
class Discovered:
    url: str
    title: str | None
    version: str  # feedparser's format id, e.g. rss20, atom10, json1
    entries: int
    how: str  # self | link-rel | anchor | common-path


def parse_feed(content: bytes, url: str, content_type: str = "") -> feedparser.FeedParserDict | None:
    """Return the parsed feed if ``content`` is a feed, else None."""
    head = content[:2048].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        return None
    d = feedparser.parse(content, response_headers={"content-location": url, "content-type": content_type})
    if not d.get("version"):
        return None
    return d


def _is_comments(url: str, title: str | None) -> bool:
    blob = f"{url} {title or ''}".lower()
    return "comment" in blob


def _origin(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, "/", "", ""))


def candidates_from_html(html: bytes | str, base_url: str) -> list[tuple[str, str]]:
    """Feed URL candidates found in a page, as (url, how) in preference order."""
    tree = HTMLParser(html if isinstance(html, str) else html.decode("utf-8", "replace"))
    base_el = tree.css_first("base[href]")
    base = urljoin(base_url, base_el.attributes.get("href") or "") if base_el else base_url
    found: list[tuple[str, str]] = []
    for link in tree.css("link[href]"):
        rel = (link.attributes.get("rel") or "").lower().split()
        typ = (link.attributes.get("type") or "").lower().split(";")[0].strip()
        if "alternate" in rel and typ in FEED_TYPES:
            found.append((urljoin(base, link.attributes["href"] or ""), "link-rel"))
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if ANCHOR_HINT.search(href):
            found.append((urljoin(base, href), "anchor"))
    seen: set[str] = set()
    out = []
    for u, how in found:
        u = u.split("#")[0]
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            out.append((u, how))
    return out


def common_path_candidates(page_url: str) -> list[tuple[str, str]]:
    p = urlsplit(page_url)
    bases = []
    path = p.path if p.path.endswith("/") else p.path + "/"
    if path not in ("", "/"):
        bases.append(urlunsplit((p.scheme, p.netloc, path, "", "")))
    bases.append(_origin(page_url))
    out = []
    for b in bases:
        for c in COMMON_PATHS:
            out.append((urljoin(b, c), "common-path"))
    return out


async def _try(fetcher: Fetcher, url: str, how: str) -> Discovered | None:
    try:
        r = await fetcher.get(url)
    except SourceError:
        return None
    d = parse_feed(r.content, r.url, r.content_type)
    if d is None:
        return None
    return Discovered(r.url, d.feed.get("title"), d.get("version", ""), len(d.entries), how)


async def discover(fetcher: Fetcher, url: str, *, limit: int = 3) -> list[Discovered]:
    """Return up to ``limit`` working feeds for ``url``, best first."""
    try:
        page: Response = await fetcher.get(url)
    except SourceError as e:
        transient = isinstance(e, NetworkError) or e.http_status in (429, 502, 503, 504)
        if transient:  # site down or rate-limiting: retry with backoff, don't report "no feed"
            raise
        raise DiscoveryError(f"could not load {url}: {e}", http_status=e.http_status) from e

    d = parse_feed(page.content, page.url, page.content_type)
    if d is not None:
        return [Discovered(page.url, d.feed.get("title"), d.get("version", ""), len(d.entries), "self")]

    results: list[Discovered] = []
    tried: set[str] = set()

    async def consider(cands: list[tuple[str, str]]) -> None:
        for cand, how in cands:
            if len(results) >= limit or len(tried) >= MAX_CANDIDATES:
                return
            if cand in tried:
                continue
            tried.add(cand)
            found = await _try(fetcher, cand, how)
            if found and found.url not in {r.url for r in results}:
                results.append(found)

    in_page = candidates_from_html(page.content, page.url)
    await consider([c for c in in_page if c[1] == "link-rel"])
    if not results:
        await consider([c for c in in_page if c[1] == "anchor"])
    if not results:
        await consider(common_path_candidates(page.url))

    # comment feeds last; otherwise keep discovery order
    results.sort(key=lambda r: _is_comments(r.url, r.title))
    return results[:limit]
