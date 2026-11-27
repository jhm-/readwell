"""Build Atom feeds and OPML from the database."""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta
from html import escape
from pathlib import Path
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from feedgen.feed import FeedGenerator

from ..config import Config
from ..screen.topics import View
from ..timeutil import from_iso, now, to_iso
from . import match
from .match import ItemScores
from .sign import sign
from .tidy import tidy_html

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


@dataclass
class Links:
    """Absolute URLs for action links (readers open them in a browser)."""

    base_url: str
    data_dir: Path | None = None  # signing key lives here; None = no action links

    def vote(self, item_id: int, topic_id: str, vote: int) -> str | None:
        if self.data_dir is None:
            return None
        msg = f"fb/{item_id}/{topic_id}/{vote}"
        return f"{self.base_url.rstrip('/')}/{msg}?s={sign(self.data_dir, msg)}"

    def pick(self, item_id: int) -> str | None:
        if self.data_dir is None:
            return None
        msg = f"pick/{item_id}"
        return f"{self.base_url.rstrip('/')}/{msg}?s={sign(self.data_dir, msg)}"

    def send(self, item_id: int) -> str | None:
        if self.data_dir is None:
            return None
        msg = f"send/{item_id}"
        return f"{self.base_url.rstrip('/')}/{msg}?s={sign(self.data_dir, msg)}"


def _topic_label(cfg: Config, tid: str) -> str:
    t = cfg.topics.by_id.get(tid) if cfg.topics else None
    return t.label if t else tid


def _entry_html(conn: sqlite3.Connection, cfg: Config, item: sqlite3.Row, links: Links | None,
                sc: ItemScores | None = None, matched: dict[str, float] | None = None,
                rejected: bool = False) -> str:
    sc = sc or ItemScores()
    matched = matched if matched is not None else match.matches(cfg, item["source_id"], sc)
    small = 'style="font-size:smaller;color:#666"'
    parts = []
    reason = item["screen_reason"] if "screen_reason" in item.keys() else None
    if reason and (matched or rejected):
        parts.append(f"<p><b>Why it's here:</b> {escape(reason)}</p>" if matched else
                     f"<p><b>Screened out:</b> {escape(reason)}</p>")
    meta = []
    if matched:
        meta.append(" · ".join(f"{escape(_topic_label(cfg, t))} {v:g}" for t, v in
                               sorted(matched.items(), key=lambda kv: -kv[1])))
    meta.append(escape(_source_name(cfg, item["source_id"])))
    also = _also_in(conn, cfg, item)
    if also:
        links_html = ", ".join(f'<a href="{escape(u)}">{escape(n)}</a>' if u else escape(n) for n, u in also)
        meta.append(f"also in: {links_html}")
    if item["paywall"] == "hard":
        arch = item["archive_url"]
        meta.append("🔒 paywalled" + (f' — <a href="{escape(arch)}">archive.today copy</a>' if arch else ""))
    parts.append(f"<p {small}>{' · '.join(meta)}</p>")
    summary = item["screen_summary"] if "screen_summary" in item.keys() else None
    if summary:
        parts.append(f"<p><b>Summary:</b> {escape(summary)}</p><hr>")
    parts.append(tidy_html(_best_body(conn, item)))
    foot = []
    if item["url"]:
        host = urlsplit(item["url"]).hostname or item["url"]
        foot.append(f'<a href="{escape(item["url"])}">Read at {escape(host)}</a>')
    if links is not None and links.data_dir is not None:
        send = links.send(item["id"])
        foot.append(f'<a href="{escape(send)}">📄 Send to Supernote</a>')
        vote_topics = list(matched) or [t for t, _ in sorted(sc.llm.items(), key=lambda kv: -kv[1])[:2]]
        known = cfg.topics.by_id if cfg.topics else {}
        for tid in vote_topics:
            if tid not in known:
                continue
            up, down = links.vote(item["id"], tid, 1), links.vote(item["id"], tid, -1)
            foot.append(f'{escape(_topic_label(cfg, tid))}: <a href="{escape(up)}">👍</a> '
                        f'<a href="{escape(down)}">👎</a>')
        if known and (rejected or not any(t in known for t in vote_topics)):
            foot.append(f'<a href="{escape(links.pick(item["id"]))}">👍 belongs in…</a>')
    if foot:
        parts.append(f"<p {small}>{' · '.join(foot)}</p>")
    return "".join(parts)


def _new_feed(feed_id: str, title: str, self_url: str, subtitle: str) -> FeedGenerator:
    fg = FeedGenerator()
    fg.id(TAG + feed_id)
    fg.title(title)
    fg.subtitle(subtitle)
    fg.link(href=self_url, rel="self")
    fg.generator("Readwell")
    fg.updated(now())
    return fg


def _add_item(fg: FeedGenerator, conn: sqlite3.Connection, cfg: Config, item: sqlite3.Row,
              links: Links | None = None, sc: ItemScores | None = None, matched: dict[str, float] | None = None,
              rejected: bool = False) -> None:
    fe = fg.add_entry(order="append")
    fe.id(f"{TAG}item/{item['id']}")
    fe.title(item["title"] or "(untitled)")
    if item["url"]:
        fe.link(href=item["url"], rel="alternate")
    fe.updated(from_iso(item["first_seen_at"]))
    fe.published(from_iso(item["sort_date"]))
    if item["author"]:
        fe.author({"name": item["author"]})
    if matched:
        for tid in matched:
            fe.category(term=tid, label=_topic_label(cfg, tid))
    fe.content(_entry_html(conn, cfg, item, links, sc, matched, rejected), type="html")


def _rows(conn: sqlite3.Connection, cfg: Config, where: list[str], params: list[object],
          limit: int | None = None) -> list[sqlite3.Row]:
    fs = cfg.settings.feeds
    base = [
        "s.in_config = 1",
        f"{_sort_date_sql()} >= ?",
        "i.state != 'baseline'",
        # hold new items back until their full text is in, but never more than 30 minutes
        "(i.state != 'normalized' OR i.first_seen_at < ?)",
    ]
    p: list[object] = [to_iso(now() - timedelta(days=fs.max_age_days)), to_iso(now() - timedelta(minutes=30))]
    return conn.execute(
        f"""SELECT i.*, {_sort_date_sql()} AS sort_date FROM items i JOIN sources s ON s.id = i.source_id
            WHERE {' AND '.join(base + where)} ORDER BY sort_date DESC, i.id DESC LIMIT ?""",
        (*p, *params, limit or fs.max_entries),
    ).fetchall()


def items_feed(conn: sqlite3.Connection, cfg: Config, self_url: str, *, source_id: str | None = None,
               links: Links | None = None) -> bytes:
    fs = cfg.settings.feeds
    if source_id:
        rows = _rows(conn, cfg, ["i.source_id = ?"], [source_id])
        title = f"{fs.title} — {_source_name(cfg, source_id)}"
        fid, subtitle = f"source/{source_id}", "Everything from one source (for checking it)"
    else:
        rows = _rows(conn, cfg, ["i.is_lead = 1"], [])
        title, fid, subtitle = f"{fs.title} — All", "all", "Every source, de-duplicated, unfiltered"
    scores = match.load_scores(conn, [r["id"] for r in rows])
    fg = _new_feed(fid, title, self_url, subtitle)
    for r in rows:
        _add_item(fg, conn, cfg, r, links, scores[r["id"]])
    return fg.atom_str(pretty=True)


def _cap_per_day(cfg: Config, picked: list[tuple[sqlite3.Row, dict[str, float]]], topic_id: str | None):
    """Apply each source's max_per_day: keep its best-scoring items per day."""
    caps = {s.id: s.filter.max_per_day for s in cfg.sources.values() if s.filter.max_per_day}
    if not caps:
        return picked
    buckets: dict[tuple[str, str], list] = defaultdict(list)
    for r, m in picked:
        if r["source_id"] in caps:
            buckets[(r["source_id"], r["sort_date"][:10])].append((r, m))
    drop = set()
    for (sid, _), entries in buckets.items():
        best = sorted(entries, key=lambda e: -(e[1].get(topic_id, 0) if topic_id else max(e[1].values(), default=0)))
        drop |= {e[0]["id"] for e in best[caps[sid]:]}
    return [(r, m) for r, m in picked if r["id"] not in drop]


def topic_feed(conn: sqlite3.Connection, cfg: Config, topic_id: str, self_url: str,
               links: Links | None = None) -> bytes:
    fs = cfg.settings.feeds
    rows = _rows(conn, cfg, [
        "i.is_lead = 1",
        "(i.id IN (SELECT item_id FROM scores WHERE topic_id = ? AND stage IN ('llm', 'keywords'))"
        " OR i.id IN (SELECT item_id FROM feedback WHERE topic_id = ? AND vote = 1))",
    ], [topic_id, topic_id], limit=fs.max_entries * 3)
    scores = match.load_scores(conn, [r["id"] for r in rows])
    picked = []
    for r in rows:
        m = match.matches(cfg, r["source_id"], scores[r["id"]])
        if topic_id in m:
            picked.append((r, m))
    picked = _cap_per_day(cfg, picked, topic_id)[: fs.max_entries]
    label = _topic_label(cfg, topic_id)
    fg = _new_feed(f"topic/{topic_id}", f"{fs.title} — {label}", self_url, f"Items screened in for {label}")
    for r, m in picked:
        _add_item(fg, conn, cfg, r, links, scores[r["id"]], m)
    return fg.atom_str(pretty=True)


def view_feed(conn: sqlite3.Connection, cfg: Config, view: View, self_url: str, links: Links | None = None) -> bytes:
    fs = cfg.settings.feeds
    topics = match.view_topics(cfg, view)
    srcs = match.view_sources(cfg, view)
    conds, params = [], []
    if topics:
        marks = ",".join("?" * len(topics))
        conds.append(f"i.id IN (SELECT item_id FROM scores WHERE stage IN ('llm', 'keywords') AND topic_id IN ({marks}))"
                     f" OR i.id IN (SELECT item_id FROM feedback WHERE vote = 1 AND topic_id IN ({marks}))")
        params += [*topics, *topics]
    if srcs:
        conds.append(f"i.source_id IN ({','.join('?' * len(srcs))})")
        params += sorted(srcs)
    picked = []
    scores = {}
    if conds:
        rows = _rows(conn, cfg, ["i.is_lead = 1", "(" + " OR ".join(f"({c})" for c in conds) + ")"], params,
                     limit=fs.max_entries * 3)
        scores = match.load_scores(conn, [r["id"] for r in rows])
        for r in rows:
            m = {t: v for t, v in match.matches(cfg, r["source_id"], scores[r["id"]],
                                                min_score=view.min_score).items() if t in topics}
            if m or r["source_id"] in srcs:
                picked.append((r, m))
    if view.exclude_tags:
        skip = {s.id for s in cfg.sources.values() if set(s.tags) & set(view.exclude_tags)}
        picked = [(r, m) for r, m in picked if r["source_id"] not in skip]
    picked = picked[: fs.max_entries]
    fg = _new_feed(f"view/{view.id}", f"{fs.title} — {view.label}", self_url, f"Combined feed: {view.label}")
    for r, m in picked:
        _add_item(fg, conn, cfg, r, links, scores.get(r["id"]), m)
    return fg.atom_str(pretty=True)


def rejected_feed(conn: sqlite3.Connection, cfg: Config, self_url: str, links: Links | None = None) -> bytes:
    """Screened out, for checking the filter: below every threshold, keyword-dropped, or muted (7 days)."""
    fs = cfg.settings.feeds
    rows = _rows(conn, cfg, ["i.is_lead = 1", "i.screen_state IN ('screened', 'dropped', 'muted')",
                             "i.first_seen_at >= ?"], [to_iso(now() - timedelta(days=7))], limit=fs.max_entries * 2)
    scores = match.load_scores(conn, [r["id"] for r in rows])
    fg = _new_feed("rejected", f"{fs.title} — Rejected", self_url,
                   "Items the filter kept out of every topic. 👍 one that should have made it.")
    n = 0
    for r in rows:
        if match.matches(cfg, r["source_id"], scores[r["id"]]):
            continue
        _add_item(fg, conn, cfg, r, links, scores[r["id"]], {}, rejected=True)
        n += 1
        if n >= fs.max_entries:
            break
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
    out += _screen_problems(conn, cfg)
    for r in conn.execute("SELECT s.*, i.title FROM sends s JOIN items i ON i.id = s.item_id"
                          " WHERE s.error IS NOT NULL AND s.requested_at >= ?", (to_iso(now() - timedelta(days=2)),)):
        out.append(Problem(f"send/{r['id']}", None, f"Send to Supernote failed: {r['title'] or r['item_id']}",
                           r["requested_at"], f"<p>{escape(r['error'])}</p><p>Folder: "
                           f"<code>{escape(cfg.settings.supernote.dir)}</code></p>"))
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


def _screen_problems(conn: sqlite3.Connection, cfg: Config) -> list[Problem]:
    """Items waiting on the LLM (no key, budget spent, LLM failing), one entry per cause per day."""
    rows = conn.execute(
        "SELECT screen_reason, COUNT(*) AS n, MIN(screened_at) AS since FROM items"
        " WHERE screen_state = 'deferred' AND first_seen_at >= ? GROUP BY screen_reason",
        (to_iso(now() - timedelta(days=cfg.settings.screen.max_age_days)),),
    ).fetchall()
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        reason = r["screen_reason"] or ""
        kind = ("budget" if "budget" in reason else "key" if "key" in reason else
                "off" if "turned off" in reason else "error")
        groups[kind].append(r)
    out = []
    titles = {"budget": "Daily LLM budget reached — items waiting", "key": "LLM not set up — items waiting",
              "off": "LLM screening is off", "error": "LLM failing — items waiting"}
    for kind, rs in groups.items():
        n = sum(r["n"] for r in rs)
        since = min((r["since"] for r in rs if r["since"]), default=to_iso(now()))
        detail = (f"<p>{n} item(s) haven't been screened yet. They'll be screened automatically once this"
                  " clears; until then they're in all.atom but not in topic feeds.</p><ul>"
                  + "".join(f"<li>{escape(r['screen_reason'] or '')} ({r['n']})</li>" for r in rs) + "</ul>")
        out.append(Problem(f"screen/{kind}/{since[:10]}", None, f"{titles[kind]} ({n})", since, detail))
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
    title = cfg.settings.feeds.title
    if cfg.views:
        vs = ET.SubElement(body, "outline", text=f"{title} views", title=f"{title} views")
        for v in cfg.views.values():
            ET.SubElement(vs, "outline", type="rss", text=f"{title} — {v.label}", title=f"{title} — {v.label}",
                          xmlUrl=f"{base}/feeds/view/{v.id}.atom")
    if cfg.topics and cfg.topics.topics:
        ts = ET.SubElement(body, "outline", text=f"{title} topics", title=f"{title} topics")
        for t in cfg.topics.topics:
            ET.SubElement(ts, "outline", type="rss", text=t.label, title=t.label,
                          xmlUrl=f"{base}/feeds/topic/{t.id}.atom")
        ET.SubElement(main, "outline", type="rss", text=f"{title} — Rejected", title=f"{title} — Rejected",
                      xmlUrl=base + "/feeds/rejected.atom")
    srcs = ET.SubElement(body, "outline", text=f"{cfg.settings.feeds.title} sources",
                         title=f"{cfg.settings.feeds.title} sources")
    for s in sorted(cfg.sources.values(), key=lambda s: s.display_name.lower()):
        if s.enabled:
            ET.SubElement(srcs, "outline", type="rss", text=s.display_name, title=s.display_name,
                          xmlUrl=f"{base}/feeds/source/{s.id}.atom")
    ET.indent(root)
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
