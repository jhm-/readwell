import asyncio
import json

import feedparser
from fastapi.testclient import TestClient

from readwell import db
from readwell.extract.document import find_act, pdf_text
from readwell.web.app import create_app
from readwell.worker import Worker

from .conftest import write_sources

TEMPLATE = "https://laws.test/{year}/{year}-{n:04d}.pdf"


def make_pdf(lines: list[str]) -> bytes:
    """A minimal, valid one-page PDF with a text layer."""
    ops = ["BT", "/F1 11 Tf", "72 720 Td", "14 TL"]
    for ln in lines:
        ops.append("(" + ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") + ") Tj T*")
    ops.append("ET")
    stream = "\n".join(ops).encode()
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 5 0 R >> >>"
        b" /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def oic(n: int, act: str) -> bytes:
    return make_pdf([f"ORDER-IN-COUNCIL 2026/{n:02d}", act.upper(), f"Pursuant to section 3 of the {act},",
                     "the Commissioner in Executive Council orders as follows."])


def test_pdf_text_and_act():
    doc = pdf_text(oic(1, "Notaries Act"))
    assert "ORDER-IN-COUNCIL 2026/01" in doc.text and not doc.scanned
    assert find_act(doc.text) == "Notaries Act"
    assert find_act("Pursuant to the Quartz Mining Act, the Commissioner") == "Quartz Mining Act"


def test_probing_baseline_then_new_numbers(site, dirs):
    config_dir, data_dir = dirs
    for n, act in [(1, "Notaries Act"), (2, "Quartz Mining Act"), (4, "Placer Mining Act")]:  # 3 is skipped
        site.add(TEMPLATE.format(year=2026, n=n), oic(n, act), "application/pdf")
    # a site that answers missing numbers with an HTML page, not a 404
    site.add(TEMPLATE.format(year=2026, n=6), "<html>Not found</html>", "text/html")
    write_sources(config_dir, "s.yaml", [{
        "id": "oic", "kind": "numbered", "url_template": TEMPLATE, "start": {"year": 2026, "n": 1},
        "title": "Test OIC {year}/{n:02d}", "probe_ahead": 3, "year_rollover": False, "doc_type": "oic",
    }])
    w = Worker(config_dir, data_dir, transport=site.transport)
    (o,) = asyncio.run(w.run_once())
    assert o.status == "ok" and o.stats.new == 3  # found 1, 2 and 4 across the gap at 3

    conn = db.connect(db.db_path(data_dir))
    row = conn.execute("SELECT cursor, baseline_done FROM sources WHERE id = 'oic'").fetchone()
    assert json.loads(row["cursor"]) == {"year": 2026, "n": 4} and row["baseline_done"] == 1
    titles = {r["title"]: r["state"] for r in conn.execute("SELECT title, state FROM items")}
    assert titles["Test OIC 2026/02 — Quartz Mining Act"] == "baseline"

    client = TestClient(create_app(config_dir, data_dir))
    assert feedparser.parse(client.get("/feeds/all.atom").content).entries == []  # back catalogue not published

    site.add(TEMPLATE.format(year=2026, n=5), oic(5, "Mineral Tenure Act"), "application/pdf")
    (o,) = asyncio.run(w.run_once())
    assert o.stats.new == 1
    entries = feedparser.parse(client.get("/feeds/all.atom").content).entries
    assert [e.title for e in entries] == ["Test OIC 2026/05 — Mineral Tenure Act"]
    assert "Commissioner in Executive Council" in entries[0].content[0].value
    assert entries[0].link == TEMPLATE.format(year=2026, n=5)


def test_year_rollover(site, dirs, monkeypatch):
    from datetime import UTC, datetime

    from readwell.sources import numbered

    config_dir, data_dir = dirs
    site.add(TEMPLATE.format(year=2026, n=1), oic(1, "Notaries Act"), "application/pdf")
    site.add(TEMPLATE.format(year=2027, n=1), oic(1, "Lands Act"), "application/pdf")
    monkeypatch.setattr(numbered, "now", lambda: datetime(2027, 1, 20, tzinfo=UTC))
    write_sources(config_dir, "s.yaml", [{
        "id": "oic", "kind": "numbered", "url_template": TEMPLATE, "start": {"year": 2026, "n": 1},
        "probe_ahead": 2, "baseline": False,
    }])
    (o,) = asyncio.run(Worker(config_dir, data_dir, transport=site.transport).run_once())
    assert o.stats.new == 2
    conn = db.connect(db.db_path(data_dir))
    assert json.loads(conn.execute("SELECT cursor FROM sources").fetchone()["cursor"]) == {"year": 2027, "n": 1}


def test_date_pattern_and_whole_issue_titles(site, dirs):
    config_dir, data_dir = dirs
    pdf = make_pdf(["ORDER OF THE LIEUTENANT GOVERNOR IN COUNCIL", "Order in Council No. 66 , Approved and Ordered March 9, 2026",
                    "Act and section: Mineral Tenure Act, s. 66"])
    site.add("https://bc.test/oic/0001_2026", pdf, "application/pdf")
    write_sources(config_dir, "s.yaml", [{
        "id": "bc", "kind": "numbered", "url_template": "https://bc.test/oic/{n:04d}_{year}",
        "start": {"year": 2026, "n": 1}, "title": "BC OIC {n}/{year}", "probe_ahead": 1, "baseline": False,
        "year_rollover": False, "date_pattern": "Approved and Ordered\\s+([A-Z][a-z]+\\s+\\d{1,2}\\s*,\\s*\\d{4})",
    }, {
        "id": "gazette", "kind": "numbered", "url_template": "https://bc.test/oic/{n:04d}_{year}",
        "start": {"year": 2026, "n": 1}, "title": "Gazette {year}-{n:02d}", "append_act": False, "probe_ahead": 1,
        "baseline": False, "year_rollover": False,
    }])
    asyncio.run(Worker(config_dir, data_dir, transport=site.transport).run_once())
    conn = db.connect(db.db_path(data_dir))
    rows = {r["source_id"]: r for r in conn.execute("SELECT source_id, title, published_at FROM items")}
    assert rows["bc"]["title"] == "BC OIC 1/2026 — Mineral Tenure Act"
    assert rows["bc"]["published_at"] == "2026-03-09T12:00:00Z"
    assert rows["gazette"]["title"] == "Gazette 2026-01"


def test_bot_challenge_is_an_error_not_a_miss(site, dirs):
    config_dir, data_dir = dirs
    site.add(TEMPLATE.format(year=2026, n=1), "<html><title>Just a moment...</title>challenge-platform</html>",
             "text/html", status=403)
    write_sources(config_dir, "s.yaml", [{"id": "oic", "kind": "numbered", "url_template": TEMPLATE,
                                          "start": {"year": 2026, "n": 1}}])
    (o,) = asyncio.run(Worker(config_dir, data_dir, transport=site.transport).run_once())
    assert o.status == "error" and "bot-check" in o.error
