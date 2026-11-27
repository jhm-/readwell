"""``kind: feed`` — RSS, Atom, RDF and JSON Feed via feedparser."""

from __future__ import annotations

import asyncio
import hashlib

from ..config import SourceCfg
from ..discovery import discover, parse_feed
from ..fetch.http import Fetcher
from ..models import DiscoveryError, FetchResult, NetworkError, ParseError, RawItem
from ..timeutil import struct_to_iso
from .base import SourceContext, register

DISCOVERY_BUDGET_S = 180  # a slow or tarpitting site shouldn't hold a worker slot for many minutes


async def resolve(fetcher: Fetcher, cfg: SourceCfg) -> str:
    if cfg.feed_url:
        return cfg.feed_url
    assert cfg.url
    try:
        found = await asyncio.wait_for(discover(fetcher, cfg.url, limit=1), timeout=DISCOVERY_BUDGET_S)
    except TimeoutError as e:
        raise NetworkError(f"feed discovery for {cfg.url} took over {DISCOVERY_BUDGET_S} s") from e
    if not found:
        raise DiscoveryError(
            f"no feed found at {cfg.url}; set 'feed_url' for this source, or give it a recipe (kind: listing)"
        )
    return found[0].url


async def fetch(
    fetcher: Fetcher,
    cfg: SourceCfg,
    *,
    resolved_url: str | None,
    etag: str | None,
    last_modified: str | None,
) -> FetchResult:
    newly_resolved = None
    feed_url = cfg.feed_url or resolved_url
    if not feed_url:
        feed_url = newly_resolved = await resolve(fetcher, cfg)
        etag = last_modified = None

    resp = await fetcher.get(feed_url, etag=etag, last_modified=last_modified)
    if resp.not_modified:
        return FetchResult(not_modified=True, http_status=304, etag=etag, last_modified=last_modified,
                           resolved_url=newly_resolved)

    d = parse_feed(resp.content, resp.url, resp.content_type)
    if d is None:
        # A discovered feed that stopped being a feed: clear it so the next run rediscovers.
        raise ParseError(
            f"{resp.url} is no longer a parseable feed ({resp.content_type or 'unknown type'})",
            clear_resolved=not cfg.feed_url,
        )

    items = [_entry(e) for e in d.entries]
    return FetchResult(
        items=items,
        http_status=resp.status,
        etag=resp.headers.get("etag"),
        last_modified=resp.headers.get("last-modified"),
        resolved_url=newly_resolved,
        feed_title=(d.feed.get("title") or None),
        site_url=(d.feed.get("link") or None),
    )


@register("feed")
async def fetch_feed(ctx: SourceContext) -> FetchResult:
    return await fetch(ctx.fetcher, ctx.source, resolved_url=ctx.row["resolved_url"], etag=ctx.row["etag"],
                       last_modified=ctx.row["last_modified"])


def _entry(e) -> RawItem:  # e: feedparser entry
    url = e.get("link") or None
    title = (e.get("title") or "").strip() or None
    published = struct_to_iso(e.get("published_parsed")) or struct_to_iso(e.get("updated_parsed"))
    content_html = None
    if e.get("content"):
        # prefer the longest html/xhtml body
        bodies = [c for c in e.content if "html" in (c.get("type") or "html")]
        if bodies:
            content_html = max(bodies, key=lambda c: len(c.get("value") or "")).get("value")
    summary = e.get("summary") or None
    guid = e.get("id") or url
    if not guid:
        guid = "h:" + hashlib.sha1(f"{title}|{published}|{summary}".encode()).hexdigest()
    return RawItem(
        guid=guid,
        url=url,
        title=title,
        author=(e.get("author") or None),
        published_at=published,
        summary_html=summary,
        content_html=content_html,
    )
