import asyncio

import pytest

from readwell.config import FetchCfg
from readwell.discovery import candidates_from_html, discover
from readwell.fetch.http import Fetcher, parse_retry_after
from readwell.models import DiscoveryError, HttpError

from .conftest import fixture


def run(coro):
    return asyncio.run(coro)


def fetcher(site):
    return Fetcher(FetchCfg(per_host_delay_s=0), site.transport)


def test_link_rel_preferred_and_comments_last(site):
    site.add("https://ex.com/", fixture("page_with_link.html"))
    site.add("https://ex.com/feed.xml", fixture("rss.xml"), "application/rss+xml")
    site.add("https://ex.com/comments/feed", fixture("rss.xml"), "application/rss+xml")

    async def go():
        async with fetcher(site) as f:
            return await discover(f, "https://ex.com/", limit=3)

    found = run(go())
    assert [d.url for d in found] == ["https://ex.com/feed.xml", "https://ex.com/comments/feed"]
    assert found[0].how == "link-rel" and found[0].entries == 2


def test_url_that_is_already_a_feed(site):
    site.add("https://ex.com/rss", fixture("atom.xml"), "application/atom+xml")

    async def go():
        async with fetcher(site) as f:
            return await discover(f, "https://ex.com/rss")

    (d,) = run(go())
    assert d.how == "self" and d.version == "atom10"


def test_common_path_fallback_under_section_then_root(site):
    site.add("https://ex.com/news/north", fixture("page_plain.html"))
    site.add("https://ex.com/feed", fixture("rss.xml"), "application/rss+xml")

    async def go():
        async with fetcher(site) as f:
            return await discover(f, "https://ex.com/news/north")

    (d,) = run(go())
    assert d.url == "https://ex.com/feed" and d.how == "common-path"
    tried = [str(r.url) for r in site.requests]
    assert tried.index("https://ex.com/news/north/feed") < tried.index("https://ex.com/feed")


def test_nothing_found_returns_empty(site):
    site.add("https://ex.com/", fixture("page_plain.html"))

    async def go():
        async with fetcher(site) as f:
            return await discover(f, "https://ex.com/")

    assert run(go()) == []


def test_unreachable_page_raises_discovery_error(site):
    async def go():
        async with fetcher(site) as f:
            return await discover(f, "https://ex.com/missing")

    with pytest.raises(DiscoveryError):
        run(go())


def test_candidates_resolve_relative_and_anchor_hints():
    html = '<a href="/rss/news.xml">RSS</a><a href="/about">About</a><link rel="alternate" type="application/atom+xml" href="atom.xml">'
    c = candidates_from_html(html, "https://ex.com/section/")
    assert ("https://ex.com/section/atom.xml", "link-rel") in c
    assert ("https://ex.com/rss/news.xml", "anchor") in c
    assert all("about" not in u for u, _ in c)


def test_retry_after_parsing():
    assert parse_retry_after("120") == 120
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None


def test_server_errors_are_transient_not_discovery_failures(site):
    site.add("https://ex.com/", "busy", status=503)

    async def go():
        async with fetcher(site) as f:
            return await discover(f, "https://ex.com/")

    with pytest.raises(HttpError) as ei:
        run(go())
    assert not isinstance(ei.value, DiscoveryError)
