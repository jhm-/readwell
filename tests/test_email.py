import asyncio
import base64
import json
from email.message import EmailMessage

import feedparser
from fastapi.testclient import TestClient

from readwell import db
from readwell.sources import mail
from readwell.web.app import create_app
from readwell.worker import Worker

from .conftest import write_sources

IMAP = {"host": "imap.test", "user": "news@test", "password_env": "TEST_IMAP_PW"}


def make_msg(subject, sender, html=None, text=None, mid="<1@test>"):
    m = EmailMessage()
    m["Subject"], m["From"], m["Message-ID"] = subject, sender, mid
    m["Date"] = "Thu, 26 Nov 2026 12:00:00 +0000"
    if html:
        m.set_content(text or "plain")
        m.add_alternative(html, subtype="html")
    else:
        m.set_content(text or "")
    return m.as_bytes()


SUBSTACK_HTML = (
    '<html><body><img src="https://t.test/open.gif" width="1" height="1">'
    '<h1><a href="https://northern.substack.com/p/the-week-in-claims?utm_source=email">The week in claims</a></h1>'
    + "".join(f"<p>Full paid paragraph {n} with plenty of words about staking and tenure.</p>" for n in range(30))
    + '<p><a href="https://substack.com/redirect/2/' + base64.urlsafe_b64encode(
        json.dumps({"e": "https://example.org/report"}).encode()).decode().rstrip("=")
    + '.sig">source report</a></p>'
    "<p>Subscribe now</p><p><a href='https://northern.substack.com/action/disable_email'>Unsubscribe</a></p>"
    "</body></html>"
)


def test_parse_substack_email():
    item = mail.parse_message(make_msg("The week in claims", "Northern Notes <northern@substack.com>",
                                       html=SUBSTACK_HTML))
    assert item.url == "https://northern.substack.com/p/the-week-in-claims"
    assert item.author == "Northern Notes <northern@substack.com>"
    assert item.guid == "mid:<1@test>" and item.doc_type == "newsletter"
    assert "open.gif" not in item.content_html
    assert "Unsubscribe" not in item.content_html and "Subscribe now" not in item.content_html
    assert "https://example.org/report" in item.content_html  # tracking redirect unwrapped
    assert "Full paid paragraph 29" in item.content_html


def test_plain_text_and_confirmation_subject():
    item = mail.parse_message(make_msg("Please confirm your subscription", "alerts@court.test",
                                       text="Hello\n\nClick to confirm."))
    assert item.title.startswith("[Action needed]")
    assert item.content_html == "<p>Hello</p><p>Click to confirm.</p>"


def test_web_url_from_view_in_browser_link():
    html = '<a href="https://list.test/track?url=https%3A%2F%2Fnews.test%2Fissue%2F42">View this email in your browser</a>'
    assert mail.web_url(html) == "https://news.test/issue/42"


def test_email_sources_split_a_mailbox_and_merge_with_rss(site, dirs, monkeypatch):
    config_dir, data_dir = dirs
    monkeypatch.setenv("TEST_IMAP_PW", "secret")
    msgs = [
        (11, make_msg("The week in claims", "Northern Notes <northern@substack.com>", html=SUBSTACK_HTML,
                      mid="<a@test>")),
        (12, make_msg("Judgment released", "alerts@court.test", text="R v X decided today.", mid="<b@test>")),
    ]
    seen = []

    def fake_fetch(imap, password, cursor, since_days):
        assert password == "secret"
        seen.append(dict(cursor))
        last = cursor.get("last_uid", 0)
        return "777", [(u, raw) for u, raw in msgs if u > last]

    monkeypatch.setattr(mail, "fetch_mailbox", fake_fetch)
    teaser = ('<?xml version="1.0"?><rss version="2.0"><channel><title>N</title><item><title>The week in claims'
              '</title><link>https://northern.substack.com/p/the-week-in-claims</link><guid>p1</guid>'
              '<description>Teaser only. Subscribe to read.</description></item></channel></rss>')
    site.add("https://northern.substack.com/feed", teaser, "application/rss+xml")
    write_sources(config_dir, "s.yaml", [
        {"id": "northern", "name": "Northern Notes", "feed_url": "https://northern.substack.com/feed",
         "full_text": "never"},
        {"id": "substacks", "kind": "email", "imap": IMAP, "from": ["*@substack.com"]},
        {"id": "inbox", "kind": "email", "imap": IMAP, "from": ["*"]},
    ])
    w = Worker(config_dir, data_dir, transport=site.transport)
    asyncio.run(w.run_once(["northern"]))
    out = {o.source_id: o for o in asyncio.run(w.run_once(["substacks", "inbox"]))}
    assert out["substacks"].stats.new == 1 and out["inbox"].stats.new == 1  # split by sender, no overlap

    conn = db.connect(db.db_path(data_dir))
    cur = conn.execute("SELECT cursor FROM sources WHERE id = 'inbox'").fetchone()["cursor"]
    assert json.loads(cur) == {"uidvalidity": "777", "last_uid": 12}
    asyncio.run(w.run_once(["inbox"]))
    assert seen[-1] == {"uidvalidity": "777", "last_uid": 12}  # resumes where it left off

    entries = feedparser.parse(TestClient(create_app(config_dir, data_dir)).get("/feeds/all.atom").content).entries
    claims = [e for e in entries if e.title == "The week in claims"]
    assert len(claims) == 1  # RSS teaser and emailed post are one story
    assert "Full paid paragraph 29" in claims[0].content[0].value  # and the full emailed text wins


def test_missing_password_is_a_clear_error(site, dirs, monkeypatch):
    config_dir, data_dir = dirs
    monkeypatch.delenv("TEST_IMAP_PW", raising=False)
    write_sources(config_dir, "s.yaml", [{"id": "inbox", "kind": "email", "imap": IMAP}])
    (o,) = asyncio.run(Worker(config_dir, data_dir, transport=site.transport).run_once())
    assert o.status == "error" and "TEST_IMAP_PW" in o.error
