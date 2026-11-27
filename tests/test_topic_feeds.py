import asyncio
import re

import feedparser
import yaml
from fastapi.testclient import TestClient

from readwell.web.app import create_app
from readwell.worker import Worker

from .conftest import write_sources
from .test_screen import FakeClaude, rss, setup


def links(html: str) -> dict[str, str]:
    return {m.group(2): m.group(1).replace("&amp;", "&")
            for m in re.finditer(r'<a href="([^"]+)">([^<]+)</a>', html)}


def build(site, dirs):
    config_dir, data_dir = dirs
    setup(config_dir)
    (config_dir / "views.yaml").write_text(yaml.safe_dump({"views": {
        "main": {"name": "Main", "topics": "all", "tags": ["general-interest"]},
        "work": {"name": "Work", "topics": ["tenure"]},
    }}))
    site.add("https://news.test/rss", rss([
        ("Yukon pauses staking near Dawson", "staking claims"),
        ("Prince Rupert port expansion approved", "port"),
        ("Port of Vancouver staking claim office opens", "staking port"),
    ]), "application/rss+xml")
    site.add("https://fun.test/rss", rss([("Crossword answers", "none")], host="fun.test"), "application/rss+xml")
    write_sources(config_dir, "s.yaml", [
        {"id": "news", "name": "News", "feed_url": "https://news.test/rss", "full_text": "never"},
        {"id": "fun", "feed_url": "https://fun.test/rss", "full_text": "never", "tags": ["general-interest"],
         "filter": {"mode": "none"}},
    ])
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport,
                       llm_transport=FakeClaude().transport).run_once())
    return TestClient(create_app(config_dir, data_dir))


def titles(client, path):
    return [e.title for e in feedparser.parse(client.get(path).content).entries]


def test_topic_view_rejected_feeds_and_votes(site, dirs):
    client = build(site, dirs)
    tenure = feedparser.parse(client.get("/feeds/topic/tenure.atom").content).entries
    assert {e.title for e in tenure} == {"Yukon pauses staking near Dawson", "Port of Vancouver staking claim office opens"}
    body = tenure[0].content[0].value
    assert "Why it's here:" in body and "Mineral tenure 9" in body
    assert "📄 Send to Supernote" in body and "👍" in body
    assert titles(client, "/feeds/topic/ports.atom") == []           # scored 4 < threshold 6
    assert set(titles(client, "/feeds/view/main.atom")) == {
        "Yukon pauses staking near Dawson", "Port of Vancouver staking claim office opens", "Crossword answers"}
    assert "Crossword answers" not in titles(client, "/feeds/view/work.atom")
    rejected = feedparser.parse(client.get("/feeds/rejected.atom").content).entries
    assert [e.title for e in rejected] == ["Prince Rupert port expansion approved"]

    # 👍 from the rejected feed pulls it into the topic; 👎 takes one out
    up = links(rejected[0].content[0].value)["👍"]
    r = client.get(up.replace("http://readwell.test:8080", ""))
    assert r.status_code == 200 and "Marked as relevant" in r.text
    assert "Prince Rupert port expansion approved" in titles(client, "/feeds/topic/ports.atom")
    down = links(next(e for e in tenure if e.title.startswith("Port of")).content[0].value)
    client.get([u for k, u in down.items() if k == "👎"][0].replace("http://readwell.test:8080", ""))
    assert titles(client, "/feeds/topic/tenure.atom") == ["Yukon pauses staking near Dawson"]

    # signatures are required, and votes become examples for the LLM
    assert client.get("/fb/1/tenure/1?s=nope").status_code == 403
    from readwell import db
    from readwell.config import load_config
    from readwell.screen.run import examples
    config_dir, data_dir = dirs
    ex = examples(db.connect(db.db_path(data_dir)), load_config(config_dir, data_dir), ["tenure", "ports"])
    assert ex["ports"][0].vote == 1 and ex["tenure"][0].vote == -1


def test_opml_and_status_show_topics_and_budget(site, dirs):
    client = build(site, dirs)
    o = client.get("/opml").text
    assert "/feeds/topic/tenure.atom" in o and "/feeds/view/main.atom" in o and "/feeds/rejected.atom" in o
    page = client.get("/").text
    assert "spent today $0.00" in page and "Mineral tenure" in page


def test_health_reports_waiting_items(site, dirs):
    config_dir, data_dir = dirs
    setup(config_dir, {"daily_budget_usd": 0.00001})
    site.add("https://news.test/rss", rss([("Staking news", "claims")]), "application/rss+xml")
    write_sources(config_dir, "s.yaml", [{"id": "news", "feed_url": "https://news.test/rss", "full_text": "never"}])
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport,
                       llm_transport=FakeClaude().transport).run_once())
    health = feedparser.parse(TestClient(create_app(config_dir, data_dir)).get("/feeds/health.atom").content)
    assert any("budget" in e.title.lower() for e in health.entries)


def test_send_to_supernote_writes_a_pdf(site, dirs, tmp_path):
    from pypdf import PdfReader

    from readwell import db
    from readwell.config import load_config
    from readwell.supernote import process_sends

    config_dir, data_dir = dirs
    client = build(site, dirs)
    cfg_doc = yaml.safe_load((config_dir / "config.yaml").read_text())
    out = tmp_path / "Saved Articles"
    out.mkdir()
    cfg_doc["supernote"] = {"dir": str(out), "page_size": "A5"}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(cfg_doc))

    entry = feedparser.parse(client.get("/feeds/topic/tenure.atom").content).entries[0]
    send = links(entry.content[0].value)["📄 Send to Supernote"].replace("http://readwell.test:8080", "")
    assert "Sent ✓" in client.get(send).text
    assert client.get(send.split("?")[0] + "?s=bad").status_code == 403

    conn = db.connect(db.db_path(data_dir))
    assert asyncio.run(process_sends(conn, load_config(config_dir, data_dir))) == 1
    (pdf,) = out.glob("*.pdf")
    assert pdf.name.endswith(f" {entry.title}.pdf")
    text = PdfReader(str(pdf)).pages[0].extract_text()
    assert entry.title.split()[0] in text and "Why it's here" in text
    assert conn.execute("SELECT done_at FROM sends").fetchone()[0]


def test_send_waits_when_the_nas_is_away(site, dirs, tmp_path):
    from readwell import db
    from readwell.config import load_config
    from readwell.supernote import process_sends

    config_dir, data_dir = dirs
    client = build(site, dirs)
    cfg_doc = yaml.safe_load((config_dir / "config.yaml").read_text())
    cfg_doc["supernote"] = {"dir": str(tmp_path / "not-mounted")}
    (config_dir / "config.yaml").write_text(yaml.safe_dump(cfg_doc))
    entry = feedparser.parse(client.get("/feeds/topic/tenure.atom").content).entries[0]
    client.get(links(entry.content[0].value)["📄 Send to Supernote"].replace("http://readwell.test:8080", ""))
    conn = db.connect(db.db_path(data_dir))
    assert asyncio.run(process_sends(conn, load_config(config_dir, data_dir))) == 0
    row = conn.execute("SELECT done_at, error FROM sends").fetchone()
    assert row["done_at"] is None and "NAS" in row["error"]


def test_cli_screening_report(site, dirs, capsys):
    from readwell import cli

    config_dir, data_dir = dirs
    build(site, dirs)
    assert cli.main(["--config-dir", str(config_dir), "--data-dir", str(data_dir), "screening"]) == 0
    out = capsys.readouterr().out
    assert "spent $0.0009" in out and "tenure" in out and "screened" in out


def test_rejected_items_without_scores_get_a_topic_picker(site, dirs):
    client = build(site, dirs)
    # a keyword-dropped item: no topic scores at all
    config_dir, data_dir = dirs
    site.add("https://town.test/rss", rss([("Council debates new library hours", "books")], host="town.test"),
             "application/rss+xml")
    write_sources(config_dir, "t.yaml", [{"id": "town", "feed_url": "https://town.test/rss", "full_text": "never"}])
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport,
                       llm_transport=FakeClaude().transport).run_once())
    entry = next(e for e in feedparser.parse(client.get("/feeds/rejected.atom").content).entries
                 if e.title.startswith("Council"))
    pick = links(entry.content[0].value)["👍 belongs in…"].replace("http://readwell.test:8080", "")
    page = client.get(pick).text
    assert "Mineral tenure" in page and "Ports &amp; rail" in page
    assert client.get(pick.split("?")[0] + "?s=x").status_code == 403
    vote = re.search(r'href="([^"]+)">Ports &amp; rail', page).group(1).replace("&amp;", "&")
    client.get(vote.replace("http://readwell.test:8080", ""))
    assert "Council debates new library hours" in titles(client, "/feeds/topic/ports.atom")
    assert "✓" in client.get(pick).text


def test_rescreen_that_drops_an_item_clears_its_old_matches(site, dirs):
    from readwell import db
    from readwell.screen.run import reset_for_rescreen

    client = build(site, dirs)
    config_dir, data_dir = dirs
    assert "Yukon pauses staking near Dawson" in titles(client, "/feeds/topic/tenure.atom")
    write_sources(config_dir, "s.yaml", [
        {"id": "news", "name": "News", "feed_url": "https://news.test/rss", "full_text": "never",
         "filter": {"mute": ["Dawson"]}},
        {"id": "fun", "feed_url": "https://fun.test/rss", "full_text": "never", "tags": ["general-interest"],
         "filter": {"mode": "none"}},
    ])
    reset_for_rescreen(db.connect(db.db_path(data_dir)), 3)
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport,
                       llm_transport=FakeClaude().transport).run_once([]))
    assert "Yukon pauses staking near Dawson" not in titles(client, "/feeds/topic/tenure.atom")
