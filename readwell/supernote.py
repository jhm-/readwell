"""Send to Supernote: render an item as a clean PDF into the NAS folder the tablet syncs.

Replaces the Instapaper → Pi scraper → PDF hop. The 📄 link in a feed entry queues a row in
``sends``; the worker renders it here and writes ``{item id} {title}.pdf`` to
``supernote.dir`` (default ``/mnt/Data/Supernote/Document/Saved Articles``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .config import Config
from .db import tx
from .timeutil import now_iso

log = logging.getLogger(__name__)
MAX_IMAGE_BYTES = 5_000_000

CSS = """
@page { size: %(size)s; margin: 16mm 14mm 18mm; @bottom-center { content: counter(page); font-size: 9pt; color: #777; } }
html { font-family: "Noto Serif", "DejaVu Serif", Georgia, serif; font-size: 12pt; line-height: 1.45; color: #000; }
h1 { font-size: 20pt; line-height: 1.2; margin: 0 0 6pt; }
h2, h3, h4 { line-height: 1.25; margin: 14pt 0 4pt; }
.meta { font-family: "Noto Sans", "DejaVu Sans", sans-serif; font-size: 9pt; color: #444; margin-bottom: 14pt;
        border-bottom: 0.5pt solid #999; padding-bottom: 6pt; word-break: break-all; }
.summary { border-left: 2pt solid #000; padding-left: 8pt; margin: 0 0 14pt; }
.why { font-family: "Noto Sans", "DejaVu Sans", sans-serif; font-size: 8.5pt; color: #555; margin-top: 18pt;
       border-top: 0.5pt solid #999; padding-top: 6pt; }
img { max-width: 100%%; height: auto; }
figure { margin: 8pt 0; } figcaption { font-size: 9pt; color: #444; }
pre, code { font-size: 9.5pt; white-space: pre-wrap; }
blockquote { margin: 8pt 0 8pt 10pt; padding-left: 8pt; border-left: 1pt solid #777; }
table { border-collapse: collapse; font-size: 10pt; } td, th { border: 0.5pt solid #999; padding: 2pt 4pt; }
a { color: #000; text-decoration: none; }
"""


def safe_filename(item_id: int, title: str | None) -> str:
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", title or "untitled")
    t = re.sub(r"\s+", " ", t).strip(" .") or "untitled"
    return f"{item_id} {t[:110].rstrip(' .')}.pdf"


def article_html(conn: sqlite3.Connection, cfg: Config, item: sqlite3.Row) -> str:
    from .web.feeds import _best_body, _source_name
    from .web.tidy import tidy_html

    src = _source_name(cfg, item["source_id"])
    date = (item["published_at"] or item["first_seen_at"] or "")[:10]
    meta = " · ".join(escape(x) for x in (src, date, item["url"] or "") if x)
    parts = [f"<h1>{escape(item['title'] or '(untitled)')}</h1>", f'<div class="meta">{meta}</div>']
    if item["screen_summary"]:
        parts.append(f'<p class="summary">{escape(item["screen_summary"])}</p>')
    body = tidy_html(_best_body(conn, item)) or "<p><i>Readwell has no text for this item.</i></p>"
    parts.append(body)
    tail = []
    if item["screen_reason"]:
        tail.append(f"Why it's here: {escape(item['screen_reason'])}")
    if item["paywall"] == "hard" and item["archive_url"]:
        tail.append(f"Paywalled — archive.today copy: {escape(item['archive_url'])}")
    if tail:
        parts.append(f'<div class="why">{"<br>".join(tail)}</div>')
    return (f'<!doctype html><html><head><meta charset="utf-8"><title>{escape(item["title"] or "")}</title>'
            f'<style>{CSS % {"size": cfg.settings.supernote.page_size}}</style></head><body>{"".join(parts)}</body></html>')


def _fetcher(user_agent: str):
    """Images only, over http(s), small, with our own honest user agent; everything else is refused."""
    client = httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": user_agent})

    def fetch(url: str, timeout: float = 10, ssl_context=None, **_):
        if urlsplit(url).scheme not in ("http", "https"):
            raise ValueError(f"not fetching {url[:40]}")
        with client.stream("GET", url) as r:
            ctype = r.headers.get("content-type", "").split(";")[0].strip()
            if r.status_code >= 400 or not ctype.startswith("image/"):
                raise ValueError(f"not an image: {url[:80]}")
            data = b""
            for chunk in r.iter_bytes():
                data += chunk
                if len(data) > MAX_IMAGE_BYTES:
                    raise ValueError("image too large")
        return {"string": data, "mime_type": ctype, "redirected_url": str(r.url)}

    return fetch, client


def render_pdf(html: str, user_agent: str) -> bytes:
    """CPU-bound: call via asyncio.to_thread."""
    import weasyprint  # imported late: only the worker needs it, and it needs the pango library

    fetch, client = _fetcher(user_agent)
    try:
        return weasyprint.HTML(string=html, url_fetcher=fetch).write_pdf()
    finally:
        client.close()


async def process_sends(conn: sqlite3.Connection, cfg: Config, limit: int = 5) -> int:
    rows = conn.execute(
        "SELECT s.id AS send_id, i.* FROM sends s JOIN items i ON i.id = s.item_id"
        " WHERE s.done_at IS NULL ORDER BY s.requested_at LIMIT ?", (limit,)
    ).fetchall()
    done = 0
    for r in rows:
        dest_dir = Path(cfg.settings.supernote.dir)
        try:
            html = article_html(conn, cfg, r)
            pdf = await asyncio.to_thread(render_pdf, html, cfg.settings.fetch.user_agent)
        except Exception as e:  # noqa: BLE001 — a render failure is final for this request; record it
            log.exception("send %s: render failed", r["send_id"])
            with tx(conn):
                conn.execute("UPDATE sends SET done_at = ?, error = ? WHERE id = ?",
                             (now_iso(), f"render failed: {type(e).__name__}: {e}"[:500], r["send_id"]))
            continue
        path = dest_dir / safe_filename(r["id"], r["title"])
        try:
            if not dest_dir.is_dir():
                raise FileNotFoundError(f"{dest_dir} is not there (is the NAS mounted?)")
            tmp = path.with_suffix(".pdf.part")
            tmp.write_bytes(pdf)
            tmp.replace(path)
        except OSError as e:  # NAS down: keep it queued and try again next tick
            with tx(conn):
                conn.execute("UPDATE sends SET error = ? WHERE id = ?", (f"waiting: {e}"[:500], r["send_id"]))
            log.warning("send %s: %s", r["send_id"], e)
            break
        with tx(conn):
            conn.execute("UPDATE sends SET done_at = ?, path = ?, error = NULL WHERE id = ?",
                         (now_iso(), str(path), r["send_id"]))
        log.info("sent to Supernote: %s", path.name)
        done += 1
    return done
