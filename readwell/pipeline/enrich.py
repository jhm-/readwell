"""Second pass over new items: fetch the full article when the feed only carried a snippet.

Runs a small batch per worker tick so a big first import doesn't hammer anyone.
Extraction is CPU-bound and runs one at a time in a thread, leaving the NUC to Emby.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from html import escape
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from ..config import Config
from ..db import tx
from ..extract.article import extract_article
from ..extract.document import looks_like_pdf, pdf_text
from ..extract.paywall import archive_url, detect
from ..fetch.http import Fetcher
from ..models import NetworkError, SourceError
from ..timeutil import iso_ago

log = logging.getLogger(__name__)
HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"
PDF_MAX_BYTES = 60_000_000
MAX_ATTEMPTS = 3
_cpu = asyncio.Semaphore(1)


@dataclass
class EnrichStats:
    checked: int = 0
    fetched: int = 0
    improved: int = 0
    paywalled: int = 0
    failed: int = 0


async def enrich_pending(conn: sqlite3.Connection, fetcher: Fetcher, cfg: Config, *, limit: int | None = None,
                         source_ids: list[str] | None = None) -> EnrichStats:
    ex = cfg.settings.extract
    stats = EnrichStats()
    # items too old to appear in any feed are not worth a fetch
    with tx(conn):
        conn.execute(
            "UPDATE items SET state = 'enriched' WHERE state = 'normalized' AND first_seen_at < ?",
            (iso_ago(days=ex.max_age_days),),
        )
    sql = ("SELECT i.* FROM items i JOIN sources s ON s.id = i.source_id"
           " WHERE i.state = 'normalized' AND s.in_config = 1")
    params: list[object] = []
    if source_ids:
        sql += f" AND i.source_id IN ({','.join('?' * len(source_ids))})"
        params += source_ids
    sql += " ORDER BY i.first_seen_at DESC, i.id DESC LIMIT ?"
    params.append(limit or ex.batch)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return stats
    await asyncio.gather(*(_one(conn, fetcher, cfg, r, stats) for r in rows))
    return stats


async def _one(conn: sqlite3.Connection, fetcher: Fetcher, cfg: Config, r: sqlite3.Row, stats: EnrichStats) -> None:
    stats.checked += 1
    s = cfg.sources.get(r["source_id"])
    mode = getattr(s, "full_text", "auto") if s else "auto"
    doc_link = ((s.model_extra or {}).get("document_link") if s else None) or None
    need = (
        r["url"]
        and (r["doc_type"] == "article" or doc_link or mode == "always")
        and mode != "never"
        and (mode == "always" or (r["word_count"] or 0) < cfg.settings.extract.min_words)
    )
    if not need:
        _set(conn, r["id"], state="enriched")
        return
    try:
        resp = await fetcher.get(r["url"], accept=HTML_ACCEPT)
    except SourceError as e:
        attempts = (r["attempts"] or 0) + 1
        transient = isinstance(e, NetworkError) or e.http_status in (429, 500, 502, 503, 504)
        done = not transient or attempts >= MAX_ATTEMPTS
        _set(conn, r["id"], state="enriched" if done else "normalized", attempts=attempts, error=str(e)[:500])
        stats.failed += 1
        return
    stats.fetched += 1
    if looks_like_pdf(resp.content):
        await _use_pdf(conn, r, resp.content, resp.url, stats)
        return
    if "html" not in resp.content_type:
        _set(conn, r["id"], state="enriched")
        return
    if doc_link:
        href = _find_link(resp.content, doc_link)
        if href:
            pdf_url = urljoin(resp.url, href)
            try:
                pdf = await fetcher.get(pdf_url, accept="application/pdf,*/*;q=0.5", max_bytes=PDF_MAX_BYTES)
            except SourceError as e:
                log.info("%s: document %s: %s", r["source_id"], pdf_url, e)
                _set(conn, r["id"], state="enriched", error=str(e)[:500])
                stats.failed += 1
                return
            if looks_like_pdf(pdf.content):
                await _use_pdf(conn, r, pdf.content, pdf.url, stats)
                return
        log.info("%s: no document link (%s) on %s; using the page text", r["source_id"], doc_link, resp.url)
    page = resp.content.decode(_charset(resp.headers.get("content-type", "")), "replace")
    async with _cpu:  # trafilatura sniffs the encoding itself when given bytes
        ext = await asyncio.to_thread(extract_article, resp.content, resp.url)
    paywall = detect(page, ext.words)
    fields: dict[str, object] = {"state": "enriched", "paywall": paywall}
    if paywall == "hard":
        fields["archive_url"] = archive_url(r["url"])
        stats.paywalled += 1
    if ext.html and ext.words > (r["word_count"] or 0):
        fields.update(content_html=ext.html, content_text=ext.text, word_count=ext.words,
                      content_hash=_hash(ext.text))
        if not r["author"] and ext.author:
            fields["author"] = ext.author
        stats.improved += 1
    _set(conn, r["id"], **fields)


def _find_link(html: bytes, selector: str) -> str | None:
    try:
        node = LexborHTMLParser(html.decode("utf-8", "replace")).css_first(selector)
    except Exception:  # noqa: BLE001 — a bad selector in config shouldn't stop enrichment
        return None
    return (node.attributes.get("href") or "").strip() or None if node else None


async def _use_pdf(conn: sqlite3.Connection, r: sqlite3.Row, content: bytes, url: str, stats: EnrichStats) -> None:
    async with _cpu:
        doc = await asyncio.to_thread(pdf_text, content)
    link = f'<p><a href="{escape(url)}">Open the PDF</a></p>'
    words = len(doc.text.split())
    fields: dict[str, object] = {"state": "enriched", "content_html": link + doc.html}
    if words:
        fields.update(content_text=doc.text, word_count=words, content_hash=_hash(doc.text))
        stats.improved += 1
    _set(conn, r["id"], **fields)


def _set(conn: sqlite3.Connection, item_id: int, **fields: object) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    with tx(conn):
        conn.execute(f"UPDATE items SET {cols} WHERE id = ?", (*fields.values(), item_id))


def _charset(content_type: str) -> str:
    for part in content_type.split(";"):
        k, _, v = part.strip().partition("=")
        if k.lower() == "charset" and v:
            try:
                "".encode(v.strip('"'))
                return v.strip('"')
            except LookupError:
                break
    return "utf-8"


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha1(text.lower().encode()).hexdigest()
