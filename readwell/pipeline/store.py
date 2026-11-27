"""Write normalized items to the database, handling duplicates.

Rules, in order:
1. same (source, guid)                                   → known item; refresh ``last_seen_at`` (and fill a
                                                            missing body if the feed now carries one)
2. same source, same canonical URL within the URL window  → the feed re-issued a guid; treat as known
3. other source, same canonical URL within the URL window → new row, joins that item's cluster
4. near-identical headline within the title window        → new row, joins that item's cluster
   (headlines with fewer than ``min_title_words`` meaningful words never cluster: "Weather" ≠ "Weather")
5. otherwise                                              → new row, cluster of one

A cluster's lead is its earliest item; only leads are published, with every source listed.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from ..config import DedupeCfg
from ..db import tx
from ..models import RawItem
from ..timeutil import from_iso, now_iso, to_iso
from .normalize import Normalized, normalize
from .titles import similar


@dataclass
class IngestStats:
    fetched: int = 0
    new: int = 0
    known: int = 0
    clustered: int = 0


def ingest(conn: sqlite3.Connection, source_id: str, raws: list[RawItem], cfg: DedupeCfg,
           *, now: str | None = None) -> IngestStats:
    now = now or now_iso()
    stats = IngestStats(fetched=len(raws))
    seen_guids: set[str] = set()
    with tx(conn):
        for raw in raws:
            item = normalize(raw)
            if item.guid in seen_guids:  # duplicate entries within one feed document
                continue
            seen_guids.add(item.guid)
            if _known(conn, source_id, item, cfg, now):
                stats.known += 1
                continue
            cluster_with = _find_cluster_match(conn, source_id, item, cfg, now)
            item_id = _insert(conn, source_id, item, now)
            if cluster_with is not None:
                _join_cluster(conn, cluster_with, item_id, now)
                stats.clustered += 1
            stats.new += 1
    return stats


def _window(now: str, **delta: float) -> str:
    return to_iso(from_iso(now) - timedelta(**delta))


def _known(conn: sqlite3.Connection, source_id: str, item: Normalized, cfg: DedupeCfg, now: str) -> bool:
    row = conn.execute(
        "SELECT id, word_count FROM items WHERE source_id = ? AND guid = ?", (source_id, item.guid)
    ).fetchone()
    if row is None and item.canonical_url:
        row = conn.execute(
            "SELECT id, word_count FROM items WHERE source_id = ? AND canonical_url = ? AND first_seen_at >= ?"
            " ORDER BY id LIMIT 1",
            (source_id, item.canonical_url, _window(now, days=cfg.url_window_days)),
        ).fetchone()
    if row is None:
        return False
    if item.word_count > (row["word_count"] or 0):
        conn.execute(
            "UPDATE items SET last_seen_at = ?, content_html = ?, content_text = ?, word_count = ?,"
            " content_hash = ?, summary = COALESCE(?, summary) WHERE id = ?",
            (now, item.content_html, item.content_text, item.word_count, item.content_hash, item.summary,
             row["id"]),
        )
    else:
        conn.execute("UPDATE items SET last_seen_at = ? WHERE id = ?", (now, row["id"]))
    return True


def _find_cluster_match(conn: sqlite3.Connection, source_id: str, item: Normalized, cfg: DedupeCfg,
                        now: str) -> int | None:
    """Return the id of an existing item this one duplicates, if any."""
    if item.canonical_url:
        row = conn.execute(
            "SELECT id FROM items WHERE canonical_url = ? AND source_id != ? AND first_seen_at >= ?"
            " ORDER BY id LIMIT 1",
            (item.canonical_url, source_id, _window(now, days=cfg.url_window_days)),
        ).fetchone()
        if row:
            return row["id"]
    if item.title_key:
        mine = frozenset(item.title_key.split())
        if len(mine) >= cfg.min_title_words:
            rows = conn.execute(
                "SELECT id, title_key FROM items WHERE title_key IS NOT NULL AND first_seen_at >= ? ORDER BY id",
                (_window(now, hours=cfg.title_window_hours),),
            )
            for r in rows:
                if similar(mine, frozenset(r["title_key"].split()), min_words=cfg.min_title_words,
                           containment=cfg.title_containment):
                    return r["id"]
    return None


def _insert(conn: sqlite3.Connection, source_id: str, item: Normalized, now: str) -> int:
    cur = conn.execute(
        """INSERT INTO items (source_id, guid, url, canonical_url, title, author, published_at,
                              first_seen_at, last_seen_at, summary, content_html, content_text, word_count,
                              content_hash, title_key, doc_type, state)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'normalized')""",
        (
            source_id, item.guid, item.url, item.canonical_url, item.title, item.author, item.published_at,
            now, now, item.summary, item.content_html, item.content_text, item.word_count, item.content_hash,
            item.title_key, item.doc_type,
        ),
    )
    return int(cur.lastrowid)


def _join_cluster(conn: sqlite3.Connection, existing_id: int, new_id: int, now: str) -> None:
    row = conn.execute("SELECT cluster_id FROM items WHERE id = ?", (existing_id,)).fetchone()
    cluster_id = row["cluster_id"]
    if cluster_id is None:
        cluster_id = int(conn.execute("INSERT INTO clusters (lead_item_id, created_at) VALUES (?, ?)",
                                      (existing_id, now)).lastrowid)
        conn.execute("UPDATE items SET cluster_id = ?, is_lead = 1 WHERE id = ?", (cluster_id, existing_id))
    conn.execute("UPDATE items SET cluster_id = ?, is_lead = 0 WHERE id = ?", (cluster_id, new_id))


def purge_old_content(conn: sqlite3.Connection, days: int) -> int:
    """Drop bodies of old items (titles, URLs and cluster links stay)."""
    cutoff = to_iso(from_iso(now_iso()) - timedelta(days=days))
    with tx(conn):
        cur = conn.execute(
            "UPDATE items SET content_html = NULL, content_text = NULL, summary = NULL"
            " WHERE first_seen_at < ? AND content_html IS NOT NULL",
            (cutoff,),
        )
    return cur.rowcount
