import asyncio

import feedparser
from fastapi.testclient import TestClient

from readwell import db
from readwell.web.app import create_app
from readwell.worker import Worker

from .conftest import write_sources


def page(cards):
    body = "".join(
        f'<div class="card"><h3><a href="/assets/{slug}">{title}</a></h3><p class="loc">{loc}</p>'
        f'<a class="more" href="/assets/{slug}">Read More</a></div>'
        for slug, title, loc in cards
    )
    return f"<html><body><header>Menu</header><main>{body}</main></body></html>"


RECIPE = {
    "id": "assets", "kind": "listing", "mode": "inventory", "url": "https://firm.test/assets-for-sale",
    "item": "div.card", "full_text": "never", "report_removals": True,
    "fields": {"title": {"sel": "h3"}, "link": {"sel": "h3 a", "attr": "href"}, "summary": {"sel": "p.loc"}},
}


def test_inventory_baseline_new_and_removed(site, dirs):
    config_dir, data_dir = dirs
    site.add(RECIPE["url"], page([("mill", "Gold mill, Yukon", "Whitehorse"), ("trucks", "Haul trucks", "Kamloops")]))
    write_sources(config_dir, "s.yaml", [RECIPE])
    w = Worker(config_dir, data_dir, transport=site.transport)
    client = TestClient(create_app(config_dir, data_dir))

    (o,) = asyncio.run(w.run_once())
    assert o.stats.new == 2
    assert feedparser.parse(client.get("/feeds/all.atom").content).entries == []  # baseline: silent

    site.add(RECIPE["url"], page([("mill", "Gold mill, Yukon", "Whitehorse"), ("trucks", "Haul trucks", "Kamloops"),
                                  ("claims", "Quartz claims near Dawson", "Dawson City")]))
    (o,) = asyncio.run(w.run_once())
    assert o.stats.new == 1
    entries = feedparser.parse(client.get("/feeds/all.atom").content).entries
    assert [e.title for e in entries] == ["Quartz claims near Dawson"]
    assert entries[0].link == "https://firm.test/assets/claims"

    site.add(RECIPE["url"], page([("mill", "Gold mill, Yukon", "Whitehorse"), ("claims", "Quartz claims near Dawson",
                                                                               "Dawson City")]))
    asyncio.run(w.run_once())  # trucks missing once: not yet removed
    conn = db.connect(db.db_path(data_dir))
    assert conn.execute("SELECT removed_at FROM items WHERE title = 'Haul trucks'").fetchone()[0] is None
    (o,) = asyncio.run(w.run_once())  # missing twice: removed
    assert o.stats.removed == 1
    titles = [e.title for e in feedparser.parse(client.get("/feeds/all.atom").content).entries]
    assert "Removed from listing: Haul trucks" in titles


def test_stream_mode_publishes_and_broken_recipe_is_reported(site, dirs):
    config_dir, data_dir = dirs
    site.add("https://paper.test/news", page([("a", "Council approves mine road", "Elko")]))
    write_sources(config_dir, "s.yaml", [
        {**RECIPE, "id": "paper", "mode": "stream", "url": "https://paper.test/news"},
        {**RECIPE, "id": "broken", "mode": "stream", "url": "https://paper.test/news", "item": "article.story"},
    ])
    out = {o.source_id: o for o in asyncio.run(Worker(config_dir, data_dir, transport=site.transport).run_once())}
    assert out["paper"].stats.new == 1
    assert out["broken"].status == "error" and "matched 0 items" in out["broken"].error
    client = TestClient(create_app(config_dir, data_dir))
    assert [e.title for e in feedparser.parse(client.get("/feeds/all.atom").content).entries] == [
        "Council approves mine road"]
    health = feedparser.parse(client.get("/feeds/health.atom").content).entries
    assert [e.title for e in health] == ["Feed unreadable: broken"]


def test_pagination_title_template_and_dates(site, dirs):
    config_dir, data_dir = dirs

    def table(rows):
        trs = "".join(f"<tr><td>{t}</td><td>{d}</td><td>{n}</td><td><a href='/oic/{n.replace('/', '_')}.html'>HTML</a>"
                      f"</td></tr>" for t, d, n in rows)
        return f"<table class='list'><tbody>{trs}</tbody></table>"

    site.add("https://gov.test/oic", table([("Mines Act", "November 26, 2026", "387/2026")]))
    site.add("https://gov.test/oic?page=2", table([("Water Act", "Nov 25, 2026", "386/2026")]))
    site.add("https://gov.test/oic?page=3", table([("Water Act", "Nov 25, 2026", "386/2026")]))  # repeats: stop
    write_sources(config_dir, "s.yaml", [{
        "id": "oic", "kind": "listing", "url": "https://gov.test/oic", "item": "table.list tbody tr",
        "full_text": "never", "pages": {"max": 5},
        "fields": {"title": {"sel": "td:nth-child(1)"}, "date": {"sel": "td:nth-child(2)"},
                   "number": {"sel": "td:nth-child(3)"}, "link": {"sel": "a", "attr": "href"}},
        "title_template": "OIC {number} — {title}",
    }])
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport).run_once())
    conn = db.connect(db.db_path(data_dir))
    rows = {r["title"]: r for r in conn.execute("SELECT title, url, published_at FROM items")}
    assert set(rows) == {"OIC 387/2026 — Mines Act", "OIC 386/2026 — Water Act"}
    assert rows["OIC 386/2026 — Water Act"]["published_at"] == "2026-11-25T12:00:00Z"
    assert rows["OIC 387/2026 — Mines Act"]["url"] == "https://gov.test/oic/387_2026.html"
    assert "https://gov.test/oic?page=4" not in [str(r.url) for r in site.requests]
