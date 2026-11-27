import asyncio
import json

from readwell import db
from readwell.sources.jsonapi import render
from readwell.worker import Worker

from .conftest import write_sources

ACTIVITY = [{"searchResults": [
    {"_id": "a1", "headline": "Warning letter issued", "content": "<p>The EAO issued a warning letter.</p>",
     "dateAdded": "2026-11-20T22:21:10.470Z", "publishDate": None,
     "project": {"_id": "p1", "name": "Fraser River Tunnel Project", "location": "Richmond"}},
], "count": 1}]

EAO = {"id": "eao", "kind": "json", "url": "https://eao.test/search?dataset=RecentActivity",
       "items": "0.searchResults", "guid": "{_id}", "title": "EAO · {project.name}: {headline}",
       "link": ["{nothing}", "https://eao.test/p/{project._id}/project-details"],
       "date": "{publishDate||dateAdded}", "body": "<p><i>{project.location|html}</i></p>{content}",
       "doc_type": "notice", "full_text": "never", "filter": {"mode": "none"}}


def test_templates():
    rec = {"a": {"b": [{"c": "x"}]}, "n": None, "d": 1774481071293, "t": "R&D"}
    assert render("{a.b.0.c}", rec) == "x"
    assert render("{n||a.b.0.c}-{t|html}", rec) == "x-R&amp;D"
    assert render("{d|date}", rec) == "Mar 25, 2026"
    assert render("{d}", rec) == 1774481071293      # a lone field keeps its type


def test_json_source_baseline_then_new(site, dirs):
    config_dir, data_dir = dirs
    site.add(EAO["url"], json.dumps(ACTIVITY), "application/json")
    write_sources(config_dir, "s.yaml", [EAO])
    w = Worker(config_dir, data_dir, transport=site.transport)
    (o,) = asyncio.run(w.run_once())
    assert o.status == "ok" and o.stats.new == 1
    conn = db.connect(db.db_path(data_dir))
    assert conn.execute("SELECT state FROM items").fetchone()["state"] == "baseline"

    ACTIVITY[0]["searchResults"].insert(0, {
        "_id": "a2", "headline": "Inspection record posted", "content": "Five issues of non-compliance.",
        "publishDate": "2026-11-26T19:02:19Z", "project": {"_id": "p2", "name": "Giscome Quarry", "location": "PG"}})
    site.add(EAO["url"], json.dumps(ACTIVITY), "application/json")
    asyncio.run(w.run_once())
    r = conn.execute("SELECT * FROM items WHERE guid = 'a2'").fetchone()
    assert r["title"] == "EAO · Giscome Quarry: Inspection record posted"
    assert r["url"] == "https://eao.test/p/p2/project-details" and r["published_at"] == "2026-11-26T19:02:19Z"
    assert "Five issues of non-compliance." in r["content_html"] and r["state"] != "baseline"


def test_yesab_reads_every_project_first_then_rotates(site, dirs):
    config_dir, data_dir = dirs
    projects = [{"projectId": f"id{n}", "projectNumber": f"2026-00{n:02d}", "title": f"Placer Mine {n}",
                 "proponentName": "Prospector"} for n in range(1, 6)]
    site.add("https://yesab.test/api/projects/search", json.dumps({"projects": projects}), "application/json")
    for p in projects:
        site.add(f"https://yesab.test/api/projects/{p['projectId']}/activity-feed", json.dumps([
            {"message": [{"message": "Project Submitted ", "messagePartType": "Text"},
                         {"message": "View Project Proposal", "messagePartType": "Link",
                          "linkTo": f"/projects/{p['projectId']}/documents/proposal"}],
             "activityDate": 1774474952983, "stageGrouping": "Proposal Submission", "documents": []}]),
            "application/json")
    write_sources(config_dir, "s.yaml", [{"id": "yesab", "kind": "yesab", "url": "https://yesab.test",
                                          "activity_per_run": 2, "filter": {"mode": "none"}}])
    w = Worker(config_dir, data_dir, transport=site.transport)
    (o,) = asyncio.run(w.run_once())
    assert o.stats.new == 5  # every project read on the first run, silently
    conn = db.connect(db.db_path(data_dir))
    assert {r["state"] for r in conn.execute("SELECT state FROM items")} == {"baseline"}
    t = conn.execute("SELECT title, content_html FROM items WHERE guid LIKE 'yesab:id3:%'").fetchone()
    assert t["title"] == "YESAB 2026-0003 Placer Mine 3: Project Submitted"
    assert 'href="https://yesab.test/projects/id3/documents/proposal"' in t["content_html"]

    # a new project appears: it is read first, and its recent activity is published
    import time
    new = {"projectId": "id9", "projectNumber": "2026-0099", "title": "Quartz Exploration - Rye", "proponentName": "X"}
    site.add("https://yesab.test/api/projects/search", json.dumps({"projects": [new, *projects]}), "application/json")
    site.add("https://yesab.test/api/projects/id9/activity-feed", json.dumps([
        {"message": [{"message": "Project Submitted", "messagePartType": "Text"}],
         "activityDate": int(time.time() * 1000) - 3600_000},
        {"message": [{"message": "Ancient history", "messagePartType": "Text"}], "activityDate": 1600000000000},
    ]), "application/json")
    before = len(site.requests)
    asyncio.run(w.run_once())
    feeds_read = [str(r.url) for r in site.requests[before:] if "activity-feed" in str(r.url)]
    assert len(feeds_read) == 2 and "id9" in feeds_read[0]
    rows = {r["title"]: r["state"] for r in conn.execute("SELECT title, state FROM items WHERE guid LIKE 'yesab:id9:%'")}
    assert list(rows) == ["YESAB 2026-0099 Quartz Exploration - Rye: Project Submitted"]
    assert rows["YESAB 2026-0099 Quartz Exploration - Rye: Project Submitted"] != "baseline"
