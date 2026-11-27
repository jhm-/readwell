import asyncio

import feedparser
from fastapi.testclient import TestClient

from readwell import db
from readwell.extract.paywall import archive_url, detect
from readwell.web.app import create_app
from readwell.worker import Worker

from .conftest import write_sources

ARTICLE = "<html><head><title>Story</title></head><body><nav>Home | News</nav><article><h1>Story</h1>" + "".join(
    f"<p>Paragraph {n} of the full story, with enough words to look like real reporting on the subject.</p>"
    for n in range(20)
) + "</article><footer>© Example</footer></body></html>"

PAYWALLED = (
    '<html><head><script type="application/ld+json">{"@type":"NewsArticle","isAccessibleForFree": false}</script>'
    '</head><body><article><h1>Locked</h1><p>The first sentence is free.</p>'
    '<div class="paywall">Subscribe to continue reading.</div></article></body></html>'
)


def feed_xml(items):
    entries = "".join(
        f"<item><title>{t}</title><link>{u}</link><guid>{u}</guid><description>{d}</description></item>"
        for t, u, d in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{entries}</channel></rss>'


def test_snippet_feed_gets_full_text_and_paywall_tag(site, dirs):
    config_dir, data_dir = dirs
    site.add("https://news.test/rss", feed_xml([
        ("Open story", "https://news.test/open", "A short teaser."),
        ("Locked story", "https://news.test/locked", "Another teaser."),
    ]), "application/rss+xml")
    site.add("https://news.test/open", ARTICLE)
    site.add("https://news.test/locked", PAYWALLED)
    write_sources(config_dir, "s.yaml", [{"id": "news", "feed_url": "https://news.test/rss"}])

    asyncio.run(Worker(config_dir, data_dir, transport=site.transport).run_once())

    conn = db.connect(db.db_path(data_dir))
    rows = {r["title"]: r for r in conn.execute("SELECT * FROM items")}
    assert rows["Open story"]["word_count"] > 150 and rows["Open story"]["paywall"] == "none"
    assert "Paragraph 19" in rows["Open story"]["content_html"]
    assert "Home | News" not in rows["Open story"]["content_html"]
    assert rows["Locked story"]["paywall"] == "hard"
    assert rows["Locked story"]["archive_url"] == "https://archive.ph/newest/https://news.test/locked"
    assert all(r["state"] == "enriched" for r in rows.values())

    entries = feedparser.parse(TestClient(create_app(config_dir, data_dir)).get("/feeds/all.atom").content).entries
    locked = next(e for e in entries if e.title == "Locked story")
    assert "paywalled" in locked.content[0].value and "archive.ph/newest" in locked.content[0].value


def test_full_text_never_skips_fetch(site, dirs):
    config_dir, data_dir = dirs
    site.add("https://news.test/rss", feed_xml([("Teaser", "https://news.test/a", "Short.")]), "application/rss+xml")
    write_sources(config_dir, "s.yaml", [{"id": "news", "feed_url": "https://news.test/rss", "full_text": "never"}])
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport).run_once())
    assert [str(r.url) for r in site.requests] == ["https://news.test/rss"]


def test_unenriched_items_are_held_back_briefly(site, dirs):
    config_dir, data_dir = dirs
    site.add("https://news.test/rss", feed_xml([("Teaser", "https://news.test/a", "Short.")]), "application/rss+xml")
    write_sources(config_dir, "s.yaml", [{"id": "news", "feed_url": "https://news.test/rss"}])
    w = Worker(config_dir, data_dir, transport=site.transport)
    from readwell.fetch.http import Fetcher
    from readwell.worker import run_sources

    async def fetch_only():
        async with Fetcher(w.cfg.settings.fetch, site.transport) as f:
            await run_sources(w.conn, f, w.cfg, list(w.cfg.sources.values()))

    w.reload_if_changed()
    asyncio.run(fetch_only())
    client = TestClient(create_app(config_dir, data_dir))
    assert feedparser.parse(client.get("/feeds/all.atom").content).entries == []  # waiting for full text


def test_paywall_detection_levels():
    assert detect("<p>plain page</p>", 500) == "none"
    assert detect(PAYWALLED, 10) == "hard"
    assert detect(PAYWALLED, 900) == "soft"  # markup present, but the text was there
    assert archive_url("https://x.test/a b") == "https://archive.ph/newest/https://x.test/a%20b"
