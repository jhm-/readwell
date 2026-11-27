"""Build Atom feeds and OPML from the database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from html import escape
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from feedgen.feed import FeedGenerator

from ..config import Config
from ..timeutil import from_iso, now, to_iso

TAG = "tag:readwell,2026:"


@dataclass
class Problem:
    key: str
    source_id: str | None
    title: str
    since: str
    detail_html: str


def _sort_date_sql() -> str:
    # published date, but never later than when we first saw it (feeds back-date and future-date freely)
    return "MIN(COALESCE(i.published_at, i.first_seen_at), i.first_seen_at)"


def _also_in(conn: sqlite3.Connection, cfg: Config, item: sqlite3.Row) -> list[tuple[str, str | None]]:
    if item["cluster_id"] is None:
        return []
    rows = conn.execute(
        "SELECT source_id, url FROM items WHERE cluster_id = ? AND id != ? ORDER BY first_seen_at",
        (item["cluster_id"], item["id"]),
    )
    out, seen = [], set()
    for r in rows:
        if r["source_id"] not in seen:
            seen.add(r["source_id"])
            out.append((_source_name(cfg, r["source_id"]), r["url"]))
    return out


def _source_name(cfg: Config, source_id: str) -> str:
    s = cfg.sources.get(source_id)
    return s.display_name if s else source_id


def _best_body(conn: sqlite3.Connection, item: sqlite3.Row) -> str:
    """The lead's body, unless a duplicate elsewhere carries much more of the story.

    E.g. a paid Substack post: the RSS copy is a teaser, the emailed copy is complete.
    """
    body, words = item["content_html"] or "", item["word_count"] or 0
    if item["cluster_id"] is not None:
        r = conn.execute(
            "SELECT content_html, word_count FROM items WHERE cluster_id = ? AND id != ? AND content_html IS NOT NULL"
            " ORDER BY word_count DESC LIMIT 1",
            (item["cluster_id"], item["id"]),
        ).fetchone()
        if r and (r["word_count"] or 0) > max(1.5 * words, words + 100):
            return r["content_html"]
    return body


def _entry_html(conn: sqlite3.Connection, cfg: Config, item: sqlite3.Row) -> str:
    src = escape(_source_name(cfg, item["source_id"]))
    meta = [src]
    also = _also_in(conn, cfg, item)
    if also:
        links = ", ".join(f'<a href="{escape(u)}">{escape(n)}</a>' if u else escape(n) for n, u in also)
        meta.append(f"also in: {links}")
    if item["paywall"] == "hard":
        arch = item["archive_url"]
        meta.append("🔒 paywalled" + (f' — <a href="{escape(arch)}">archive.today copy</a>' if arch else ""))
    header = f'<p style="font-size:smaller;color:#666">{" · ".join(meta)}</p>'
    body = _best_body(conn, item)
    footer = ""
    if item["url"]:
        host = urlsplit(item["url"]).hostname or item["url"]
        footer = f'<p><a href="{escape(item["url"])}">Read at {escape(host)}</a></p>'
    return header + body + footer


def _new_feed(feed_id: str, title: str, self_url: str, subtitle: str) -> FeedGenerator:
    fg = FeedGenerator()
    fg.id(TAG + feed_id)
    fg.title(title)
    fg.subtitle(subtitle)
    fg.link(href=self_url, rel="self")
    fg.generator("Readwell")
    fg.updated(now())
    return fg


def _add_item(fg: FeedGenerator, conn: sqlite3.Connection, cfg: Config, item: sqlite3.Row) -> None:
    fe = fg.add_entry(order="append")
    fe.id(f"{TAG}item/{item['id']}")
    fe.title(item["title"] or "(untitled)")
    if item["url"]:
        fe.link(href=item["url"], rel="alternate")
    fe.updated(from_iso(item["first_seen_at"]))
    fe.published(from_iso(item["sort_date"]))
    if item["author"]:
        fe.author({"name": item["author"]})
    fe.content(_entry_html(conn, cfg, item), type="html")


def items_feed(conn: sqlite3.Connection, cfg: Config, self_url: str, *, source_id: str | None = None) -> bytes:
    fs = cfg.settings.feeds
    cutoff = to_iso(now() - timedelta(days=fs.max_age_days))
    where = [
        "s.in_config = 1",
        f"{_sort_date_sql()} >= ?",
        "i.state != 'baseline'",
        # hold new items back until their full text is in, but never more than 30 minutes
        "(i.state != 'normalized' OR i.first_seen_at < ?)",
    ]
    params: list[object] = [cutoff, to_iso(now() - timedelta(minutes=30))]
    if source_id:
        where.append("i.source_id = ?")
        params.append(source_id)
        title = f"{fs.title} — {_source_name(cfg, source_id)}"
        fid, subtitle = f"source/{source_id}", "Everything from one source (for checking it)"
    else:
        where.append("i.is_lead = 1")
        title, fid, subtitle = f"{fs.title} — All", "all", "Every source, de-duplicated, unfiltered"
    rows = conn.execute(
        f"""SELECT i.*, {_sort_date_sql()} AS sort_date FROM items i JOIN sources s ON s.id = i.source_id
            WHERE {' AND '.join(where)} ORDER BY sort_date DESC, i.id DESC LIMIT ?""",
        (*params, fs.max_entries),
    ).fetchall()
    fg = _new_feed(fid, title, self_url, subtitle)
    for r in rows:
        _add_item(fg, conn, cfg, r)
    return fg.atom_str(pretty=True)


def problems(conn: sqlite3.Connection, cfg: Config) -> list[Problem]:
    out: list[Problem] = []
    threshold = cfg.settings.health.failure_threshold
    if cfg.from_cache:
        out.append(Problem("config-cache", None, "Config share unavailable — running on the cached copy",
                           to_iso(now()).split("T")[0] + "T00:00:00Z",
                           f"<p>Could not read the config directory; using <code>{escape(str(cfg.config_dir))}</code>."
                           " Check the NAS mount.</p>"))
    if cfg.errors:
        detail = "<ul>" + "".join(f"<li>{escape(e)}</li>" for e in cfg.errors) + "</ul>"
        out.append(Problem(f"config/{cfg.fingerprint[:12]}", None, f"{len(cfg.errors)} problem(s) in config files",
                           to_iso(now()).split("T")[0] + "T00:00:00Z", detail))
    rows = conn.execute(
        "SELECT * FROM sources WHERE in_config = 1 AND enabled = 1 AND consecutive_failures > 0 ORDER BY id"
    )
    for r in rows:
        immediate = r["error_kind"] in ("discovery", "empty", "parse")
        if not immediate and r["consecutive_failures"] < threshold:
            continue
        name = _source_name(cfg, r["id"])
        label = {
            "discovery": "No feed found",
            "empty": "Feed went empty",
            "parse": "Feed unreadable",
            "http": "Fetch failing",
            "network": "Unreachable",
        }.get(r["error_kind"], "Failing")
        s = cfg.sources.get(r["id"])
        detail = (
            f"<p><b>{escape(name)}</b> (<code>{escape(r['id'])}</code>)</p>"
            f"<p>{escape(r['last_error'] or '')}</p>"
            "<ul>"
            f"<li>Configured URL: {escape(r['url'] or '')}</li>"
            f"<li>Feed URL in use: {escape(r['resolved_url'] or '(none)')}</li>"
            f"<li>Consecutive failures: {r['consecutive_failures']} since {escape(r['failing_since'] or '?')}</li>"
            f"<li>Last success: {escape(r['last_ok_at'] or 'never')}</li>"
            f"<li>Next attempt: {escape(r['next_run_at'] or 'soon')}</li>"
            "</ul>"
        )
        if r["error_kind"] == "discovery" and s is not None:
            detail += "<p>Fix: add <code>feed_url:</code> to this source, or describe the page with a recipe.</p>"
        out.append(Problem(f"source/{r['id']}/{r['failing_since']}", r["id"], f"{label}: {name}",
                           r["failing_since"] or to_iso(now()), detail))
    return out


def health_feed(conn: sqlite3.Connection, cfg: Config, self_url: str) -> bytes:
    fg = _new_feed("health", f"{cfg.settings.feeds.title} — Health", self_url,
                   "Broken sources and config problems. Empty means all is well.")
    for p in problems(conn, cfg):
        fe = fg.add_entry(order="append")
        fe.id(f"{TAG}health/{p.key}")
        fe.title(p.title)
        fe.updated(from_iso(p.since))
        fe.published(from_iso(p.since))
        fe.content(p.detail_html, type="html")
    return fg.atom_str(pretty=True)


def opml(cfg: Config, base_url: str) -> bytes:
    base = base_url.rstrip("/")
    root = ET.Element("opml", version="2.0")
    head = ET.SubElement(root, "head")
    ET.SubElement(head, "title").text = cfg.settings.feeds.title
    body = ET.SubElement(root, "body")
    main = ET.SubElement(body, "outline", text=cfg.settings.feeds.title, title=cfg.settings.feeds.title)
    for text, path in (("All", "/feeds/all.atom"), ("Health", "/feeds/health.atom")):
        ET.SubElement(main, "outline", type="rss", text=f"{cfg.settings.feeds.title} — {text}",
                      title=f"{cfg.settings.feeds.title} — {text}", xmlUrl=base + path)
    srcs = ET.SubElement(body, "outline", text=f"{cfg.settings.feeds.title} sources",
                         title=f"{cfg.settings.feeds.title} sources")
    for s in sorted(cfg.sources.values(), key=lambda s: s.display_name.lower()):
        if s.enabled:
            ET.SubElement(srcs, "outline", type="rss", text=s.display_name, title=s.display_name,
                          xmlUrl=f"{base}/feeds/source/{s.id}.atom")
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
