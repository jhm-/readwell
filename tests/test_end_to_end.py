"""Worker → database → web feeds, against a fake internet."""

import asyncio
from xml.etree import ElementTree as ET

import feedparser
from fastapi.testclient import TestClient

from readwell import db
from readwell.web.app import create_app
from readwell.worker import Worker

from .conftest import fixture, write_sources


def setup_sources(site, config_dir):
    site.add("https://news.example.com/", fixture("page_with_link.html"))
    site.add("https://news.example.com/feed.xml", fixture("rss.xml"), "application/rss+xml", headers={"etag": '"v1"'})
    site.add("https://other.example.org/atom", fixture("atom.xml"), "application/atom+xml")
    write_sources(config_dir, "starter.yaml", {"defaults": {"kind": "feed"}, "sources": [
        {"id": "example-news", "name": "Example News", "url": "https://news.example.com/"},
        {"id": "other-outlet", "name": "Other Outlet", "feed_url": "https://other.example.org/atom"},
        {"id": "broken", "url": "https://broken.example.net/"},
        {"id": "later", "kind": "listing", "url": "https://later.example.net/"},
    ]})


def test_full_cycle(site, dirs):
    config_dir, data_dir = dirs
    setup_sources(site, config_dir)
    w = Worker(config_dir, data_dir, transport=site.transport)
    first = asyncio.run(w.run_once(["example-news"]))[0]  # run this one first so it owns the leads
    assert first.status == "ok" and first.stats.new == 2

    conn = db.connect(db.db_path(data_dir))
    src = conn.execute("SELECT * FROM sources WHERE id = 'example-news'").fetchone()
    assert src["resolved_url"] == "https://news.example.com/feed.xml" and src["etag"] == '"v1"'

    outcomes = {o.source_id: o for o in asyncio.run(w.run_once())}
    # second fetch of example-news is a conditional GET → 304
    assert outcomes["example-news"].status == "not_modified"
    assert any(r.headers.get("if-none-match") == '"v1"' for r in site.requests)
    other = outcomes["other-outlet"].stats
    # the Atom feed's first entry is a near-duplicate headline, the second shares a canonical URL
    assert other.new == 2 and other.clustered == 2
    assert outcomes["broken"].status == "error" and outcomes["broken"].error_kind == "discovery"
    assert "later" not in outcomes  # kinds not built yet are skipped

    client = TestClient(create_app(config_dir, data_dir))

    all_feed = feedparser.parse(client.get("/feeds/all.atom").content)
    titles = [e.title for e in all_feed.entries]
    assert len(titles) == 2, titles  # 4 items, 2 clusters of duplicates → 2 leads
    lead = next(e for e in all_feed.entries if "Ross River" in e.title)
    assert "also in" in lead.content[0].value and "Other Outlet" in lead.content[0].value
    assert "<script" not in lead.content[0].value
    assert lead.link.startswith("https://news.example.com/2026/11/withdrawal")

    src_feed = feedparser.parse(client.get("/feeds/source/other-outlet.atom").content)
    assert len(src_feed.entries) == 2  # per-source feeds show duplicates too
    assert client.get("/feeds/source/nope.atom").status_code == 404

    health = feedparser.parse(client.get("/feeds/health.atom").content)
    assert [e.title for e in health.entries] == ["No feed found: broken"]

    opml = ET.fromstring(client.get("/opml").content)
    urls = [o.get("xmlUrl") for o in opml.iter("outline") if o.get("xmlUrl")]
    assert "http://testserver/feeds/all.atom" in urls
    assert "http://testserver/feeds/source/example-news.atom" in urls

    hz = client.get("/healthz").json()
    assert hz["ok"] and hz["items"] == 4 and hz["problems"] == 1

    page = client.get("/").text
    assert "example-news" in page and "not built yet" in page


def test_http_failures_reach_health_only_after_threshold(site, dirs):
    config_dir, data_dir = dirs
    site.add("https://flaky.example/rss", b"oops", "text/plain", status=500)
    write_sources(config_dir, "a.yaml", [{"id": "flaky", "feed_url": "https://flaky.example/rss"}])
    w = Worker(config_dir, data_dir, transport=site.transport)
    client = TestClient(create_app(config_dir, data_dir))
    for _ in range(4):
        asyncio.run(w.run_once())
    assert feedparser.parse(client.get("/feeds/health.atom").content).entries == []
    asyncio.run(w.run_once())
    (entry,) = feedparser.parse(client.get("/feeds/health.atom").content).entries
    assert entry.title == "Fetch failing: flaky"

    conn = db.connect(db.db_path(data_dir))
    row = conn.execute("SELECT * FROM sources WHERE id = 'flaky'").fetchone()
    assert row["consecutive_failures"] == 5 and row["next_run_at"] is not None


def test_config_change_resets_learned_state(site, dirs):
    config_dir, data_dir = dirs
    setup_sources(site, config_dir)
    w = Worker(config_dir, data_dir, transport=site.transport)
    asyncio.run(w.run_once(["example-news"]))
    write_sources(config_dir, "starter.yaml", {"sources": [
        {"id": "example-news", "feed_url": "https://other.example.org/atom"},
    ]})
    w.reload_if_changed()
    conn = db.connect(db.db_path(data_dir))
    row = conn.execute("SELECT * FROM sources WHERE id = 'example-news'").fetchone()
    assert row["resolved_url"] is None and row["etag"] is None
    gone = conn.execute("SELECT in_config FROM sources WHERE id = 'other-outlet'").fetchone()
    assert gone["in_config"] == 0


def test_backup(site, dirs, tmp_path):
    from readwell.worker import backup_db

    config_dir, data_dir = dirs
    setup_sources(site, config_dir)
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport).run_once())
    dest = backup_db(data_dir, tmp_path / "backups", keep=14)
    assert db.connect(dest).execute("SELECT COUNT(*) FROM items").fetchone()[0] == 4


def test_web_explains_missing_config(tmp_path):
    client = TestClient(create_app(tmp_path / "missing", tmp_path / "data"))
    r = client.get("/feeds/all.atom")
    assert r.status_code == 503 and "config unavailable" in r.text


def test_rename_keeps_history_and_removed_leads_are_replaced(site, dirs):
    config_dir, data_dir = dirs
    feed = fixture("rss.xml")
    site.add("https://a.example/rss", feed, "application/rss+xml")
    site.add("https://b.example/rss", feed, "application/rss+xml")  # same feed published twice
    write_sources(config_dir, "s.yaml", [
        {"id": "first", "feed_url": "https://a.example/rss"},
        {"id": "twin", "feed_url": "https://b.example/rss"},
    ])
    w = Worker(config_dir, data_dir, transport=site.transport)
    asyncio.run(w.run_once(["first"]))
    asyncio.run(w.run_once(["twin"]))  # twin's copies cluster under first's leads
    client = TestClient(create_app(config_dir, data_dir))
    assert len(feedparser.parse(client.get("/feeds/all.atom").content).entries) == 2

    # rename "first" -> "renamed" and drop the twin
    write_sources(config_dir, "s.yaml", [
        {"id": "renamed", "name": "Renamed", "feed_url": "https://a.example/rss", "previous_ids": ["first"]},
    ])
    w.reload_if_changed()
    conn = db.connect(db.db_path(data_dir))
    assert conn.execute("SELECT COUNT(*) FROM items WHERE source_id = 'renamed'").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM sources WHERE id = 'first'").fetchone()[0] == 0
    out = {o.source_id: o for o in asyncio.run(w.run_once())}
    assert out["renamed"].stats.new == 0 and out["renamed"].stats.known == 2  # nothing re-published
    client = TestClient(create_app(config_dir, data_dir))
    entries = feedparser.parse(client.get("/feeds/all.atom").content).entries
    assert len(entries) == 2 and all("Renamed" in e.content[0].value for e in entries)

    # now remove the source that holds the leads: the twin's copies must take over
    write_sources(config_dir, "s.yaml", [{"id": "twin", "feed_url": "https://b.example/rss"}])
    w.reload_if_changed()
    client = TestClient(create_app(config_dir, data_dir))
    assert len(feedparser.parse(client.get("/feeds/all.atom").content).entries) == 2
