"""The worker: a single asyncio loop that fetches due sources and feeds the pipeline.

Each tick it reloads the config if any file changed, syncs the ``sources`` table
with the YAML, and runs every source whose ``next_run_at`` has passed. Failures
back off exponentially (``interval × 2^failures``, capped), honouring
``Retry-After``. Health problems are recorded on the source row and surfaced
by ``/feeds/health.atom``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import signal
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx

from . import db
from .config import (
    Config,
    ConfigError,
    SourceCfg,
    default_config_dir,
    default_data_dir,
    fingerprint,
    load_config,
)
from .fetch.http import Fetcher
from .models import FetchResult, HttpError, SourceError
from .pipeline.enrich import enrich_pending
from .pipeline.store import IngestStats, ingest, mark_removals, purge_old_content
from .sources import REGISTRY, SourceContext
from .timeutil import now, now_iso, to_iso

log = logging.getLogger(__name__)


@dataclass
class RunOutcome:
    source_id: str
    status: str  # ok | not_modified | error | skipped
    stats: IngestStats | None = None
    error: str | None = None
    error_kind: str | None = None


# --------------------------------------------------------------------------- source table sync


def sync_sources(conn: sqlite3.Connection, cfg: Config) -> None:
    """Make the sources table mirror the YAML. Rows for removed sources are kept (in_config=0).

    A source with ``previous_ids`` takes over the history of the first of those ids that
    exists, so renaming a source doesn't re-publish everything it already delivered.
    """
    with db.tx(conn):
        existing = {r["id"]: r for r in conn.execute("SELECT id, kind, url FROM sources")}
        for s in cfg.sources.values():
            if s.id not in existing:
                for old in s.previous_ids:
                    if old in existing and old not in cfg.sources:
                        _rename_source(conn, old, s.id)
                        existing[s.id] = existing.pop(old)
                        break
            row = existing.get(s.id)
            url = s.feed_url or s.url
            if row is None:
                conn.execute(
                    "INSERT INTO sources (id, kind, enabled, in_config, url) VALUES (?, ?, ?, 1, ?)",
                    (s.id, s.kind, int(s.enabled), url),
                )
                continue
            if row["url"] != url or row["kind"] != s.kind:
                # configuration changed: forget everything learned about the old URL
                conn.execute(
                    "UPDATE sources SET kind = ?, url = ?, resolved_url = NULL, resolved_at = NULL, etag = NULL,"
                    " last_modified = NULL, next_run_at = NULL, consecutive_failures = 0, failing_since = NULL,"
                    " error_kind = NULL, last_error = NULL WHERE id = ?",
                    (s.kind, url, s.id),
                )
            conn.execute("UPDATE sources SET enabled = ?, in_config = 1 WHERE id = ?", (int(s.enabled), s.id))
        gone = set(existing) - set(cfg.sources)
        for sid in gone:
            conn.execute("UPDATE sources SET in_config = 0 WHERE id = ?", (sid,))
        reelect_leads(conn)


def _rename_source(conn: sqlite3.Connection, old: str, new: str) -> None:
    log.info("renaming source %s -> %s (history kept)", old, new)
    conn.execute("UPDATE sources SET id = ? WHERE id = ?", (new, old))
    conn.execute("UPDATE items SET source_id = ? WHERE source_id = ?", (new, old))
    conn.execute("UPDATE runs SET source_id = ? WHERE source_id = ?", (new, old))


def reelect_leads(conn: sqlite3.Connection) -> int:
    """Give each cluster whose lead came from a removed source a new, visible lead."""
    rows = conn.execute(
        """SELECT i.cluster_id FROM items i JOIN sources s ON s.id = i.source_id
           WHERE i.is_lead = 1 AND i.cluster_id IS NOT NULL AND s.in_config = 0"""
    ).fetchall()
    moved = 0
    for r in rows:
        new = conn.execute(
            """SELECT i.id FROM items i JOIN sources s ON s.id = i.source_id
               WHERE i.cluster_id = ? AND s.in_config = 1 ORDER BY i.first_seen_at, i.id LIMIT 1""",
            (r["cluster_id"],),
        ).fetchone()
        if new is None:
            continue
        conn.execute("UPDATE items SET is_lead = 0 WHERE cluster_id = ?", (r["cluster_id"],))
        conn.execute("UPDATE items SET is_lead = 1 WHERE id = ?", (new["id"],))
        conn.execute("UPDATE clusters SET lead_item_id = ? WHERE id = ?", (new["id"], r["cluster_id"]))
        moved += 1
    return moved


def due_sources(conn: sqlite3.Connection, cfg: Config, at: str | None = None) -> list[SourceCfg]:
    at = at or now_iso()
    rows = conn.execute(
        "SELECT id FROM sources WHERE enabled = 1 AND in_config = 1 AND (next_run_at IS NULL OR next_run_at <= ?)"
        " ORDER BY next_run_at IS NOT NULL, next_run_at",
        (at,),
    )
    out = []
    for r in rows:
        s = cfg.sources.get(r["id"])
        if s is not None and s.implemented:
            out.append(s)
    return out


# --------------------------------------------------------------------------- running one source


def _next_run(interval_min: int, failures: int, max_backoff_h: float, retry_after_s: float | None) -> str:
    if failures:
        delay = min(interval_min * 60 * (2 ** min(failures - 1, 10)), max_backoff_h * 3600)
    else:
        delay = interval_min * 60 * random.uniform(0.95, 1.05)  # jitter so sources drift apart
    if retry_after_s:
        delay = max(delay, retry_after_s)
    return to_iso(now() + timedelta(seconds=delay))


async def run_source(conn: sqlite3.Connection, fetcher: Fetcher, cfg: Config, s: SourceCfg) -> RunOutcome:
    if not s.implemented:
        return RunOutcome(s.id, "skipped", error=f"kind '{s.kind}' is not implemented yet")
    row = conn.execute("SELECT * FROM sources WHERE id = ?", (s.id,)).fetchone()
    started = now_iso()
    run_id = conn.execute("INSERT INTO runs (source_id, started_at) VALUES (?, ?)", (s.id, started)).lastrowid

    try:
        result: FetchResult = await REGISTRY[s.kind](SourceContext(fetcher, cfg, s, row))
        if (result.expect_items and not result.not_modified and not result.items
                and (row["typical_count"] or 0) >= 5):
            err = SourceError(f"feed returned no items (usually {row['typical_count']}) — it may have moved")
            err.kind = "empty"
            raise err
    except SourceError as e:
        return _record_failure(conn, cfg, s, row, run_id, e)
    except (httpx.HTTPError, OSError) as e:  # anything the fetcher didn't wrap
        err = SourceError(f"{type(e).__name__}: {e}")
        err.kind = "network"
        return _record_failure(conn, cfg, s, row, run_id, err)
    except Exception as e:  # noqa: BLE001 — a bug or a parser crash in one source must not stop the others
        log.exception("%s: unexpected error", s.id)
        err = SourceError(f"internal error: {type(e).__name__}: {e}")
        err.kind = "internal"
        return _record_failure(conn, cfg, s, row, run_id, err)

    stats = None
    if not result.not_modified:
        stats = ingest(conn, s.id, result.items, cfg.settings.dedupe,
                       initial_state="baseline" if result.baseline else "normalized")
        if result.present_guids is not None:
            stats.removed = mark_removals(conn, s.id, result.present_guids, s)

    count = len(result.items)
    typical = row["typical_count"]
    if not result.not_modified and count:
        typical = count if typical is None else round(0.7 * typical + 0.3 * count)
    finished = now_iso()
    with db.tx(conn):
        conn.execute(
            """UPDATE sources SET last_run_at = ?, last_ok_at = ?, last_status = ?, consecutive_failures = 0,
                   failing_since = NULL, error_kind = NULL, last_error = NULL,
                   etag = ?, last_modified = ?, typical_count = ?,
                   resolved_url = COALESCE(?, resolved_url),
                   resolved_at = CASE WHEN ? IS NOT NULL THEN ? ELSE resolved_at END,
                   title = COALESCE(?, title), site_url = COALESCE(?, site_url), next_run_at = ?,
                   cursor = COALESCE(?, cursor), baseline_done = 1
               WHERE id = ?""",
            (
                finished, finished, result.http_status, result.etag, result.last_modified, typical,
                result.resolved_url, result.resolved_url, finished, result.feed_title, result.site_url,
                _next_run(s.interval_min, 0, cfg.settings.fetch.max_backoff_hours, None), result.cursor, s.id,
            ),
        )
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, http_status = ?, fetched = ?, new = ? WHERE id = ?",
            (finished, "not_modified" if result.not_modified else "ok", result.http_status, count,
             stats.new if stats else 0, run_id),
        )
    return RunOutcome(s.id, "not_modified" if result.not_modified else "ok", stats)


def _record_failure(conn: sqlite3.Connection, cfg: Config, s: SourceCfg, row: sqlite3.Row, run_id: int,
                    e: SourceError) -> RunOutcome:
    finished = now_iso()
    failures = (row["consecutive_failures"] or 0) + 1
    clear = e.clear_resolved or (isinstance(e, HttpError) and e.http_status in (404, 410) and not s.feed_url)
    next_run = _next_run(s.interval_min, failures, cfg.settings.fetch.max_backoff_hours, e.retry_after_s)
    with db.tx(conn):
        conn.execute(
            """UPDATE sources SET last_run_at = ?, last_status = ?, consecutive_failures = ?,
                   failing_since = COALESCE(failing_since, ?), error_kind = ?, last_error = ?, next_run_at = ?,
                   resolved_url = CASE WHEN ? THEN NULL ELSE resolved_url END,
                   etag = CASE WHEN ? THEN NULL ELSE etag END,
                   last_modified = CASE WHEN ? THEN NULL ELSE last_modified END
               WHERE id = ?""",
            (finished, e.http_status, failures, finished, e.kind, str(e)[:1000], next_run, clear, clear, clear, s.id),
        )
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = 'error', http_status = ?, error = ? WHERE id = ?",
            (finished, e.http_status, str(e)[:1000], run_id),
        )
    log.warning("%s: %s (%s, failure %d)", s.id, e, e.kind, failures)
    return RunOutcome(s.id, "error", error=str(e), error_kind=e.kind)


async def run_sources(conn: sqlite3.Connection, fetcher: Fetcher, cfg: Config,
                      sources: list[SourceCfg]) -> list[RunOutcome]:
    # Fetches run concurrently (the fetcher limits per-host and global concurrency); the
    # sqlite work in run_source is synchronous and quick, so it interleaves safely on one connection.
    return list(await asyncio.gather(*(run_source(conn, fetcher, cfg, s) for s in sources)))


# --------------------------------------------------------------------------- the loop


class Worker:
    def __init__(self, config_dir: Path | None = None, data_dir: Path | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config_dir = config_dir or default_config_dir()
        self.data_dir = data_dir or default_data_dir()
        self.transport = transport
        self.conn = db.open_db(self.data_dir)
        self.cfg: Config | None = None
        self._fp: str | None = None
        self._stop = asyncio.Event()
        self._last_purge: str | None = None

    def reload_if_changed(self) -> Config:
        fp = fingerprint(self.config_dir)
        if self.cfg is None or fp != self._fp or self.cfg.from_cache:
            try:
                cfg = load_config(self.config_dir, self.data_dir)
            except ConfigError as e:
                if self.cfg is None:
                    raise
                log.error("config reload failed, keeping previous config: %s", e)
                return self.cfg
            if self.cfg is None or cfg.fingerprint != self.cfg.fingerprint or cfg.from_cache != self.cfg.from_cache:
                log.info("config loaded from %s: %d sources%s", cfg.config_dir, len(cfg.sources),
                         " (CACHED COPY)" if cfg.from_cache else "")
            self.cfg, self._fp = cfg, fp
            sync_sources(self.conn, cfg)
        return self.cfg

    async def run_once(self, source_ids: list[str] | None = None) -> list[RunOutcome]:
        cfg = self.reload_if_changed()
        if source_ids:
            missing = [i for i in source_ids if i not in cfg.sources]
            if missing:
                raise ConfigError(f"unknown source id(s): {', '.join(missing)}")
            todo = [cfg.sources[i] for i in source_ids]
        else:
            todo = [s for s in cfg.sources.values() if s.enabled and s.implemented]
        async with Fetcher(cfg.settings.fetch, self.transport) as fetcher:
            outcomes = await run_sources(self.conn, fetcher, cfg, todo)
            ids = [s.id for s in todo]
            for _ in range(50):  # finish enriching what we just fetched
                es = await enrich_pending(self.conn, fetcher, cfg, limit=100, source_ids=ids)
                if not es.checked:
                    break
            return outcomes

    def _maybe_purge(self, cfg: Config) -> None:
        today = now_iso()[:10]
        if self._last_purge != today:
            n = purge_old_content(self.conn, cfg.settings.retention.content_days)
            if n:
                log.info("purged bodies of %d old items", n)
            with db.tx(self.conn):
                self.conn.execute("DELETE FROM runs WHERE started_at < ?", (to_iso(now() - timedelta(days=30)),))
            self._last_purge = today

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        log.info("worker started: config %s, data %s", self.config_dir, self.data_dir)
        while True:  # wait for a readable config (e.g. NAS not mounted yet) instead of crash-looping
            try:
                cfg = self.reload_if_changed()
                break
            except ConfigError as e:
                log.error("%s — retrying in 60 s", e)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=60)
                    return
                except TimeoutError:
                    pass
        fetcher = Fetcher(cfg.settings.fetch, self.transport)
        fetch_cfg = cfg.settings.fetch
        try:
            while not self._stop.is_set():
                try:
                    cfg = self.reload_if_changed()
                    if cfg.settings.fetch != fetch_cfg:
                        await fetcher.aclose()
                        fetcher, fetch_cfg = Fetcher(cfg.settings.fetch, self.transport), cfg.settings.fetch
                    due = due_sources(self.conn, cfg)
                    if due:
                        outcomes = await run_sources(self.conn, fetcher, cfg, due)
                        new = sum(o.stats.new for o in outcomes if o.stats)
                        errs = sum(1 for o in outcomes if o.status == "error")
                        log.info("ran %d sources: %d new items, %d errors", len(outcomes), new, errs)
                    es = await enrich_pending(self.conn, fetcher, cfg)
                    if es.checked:
                        log.info("enriched %d items: %d pages fetched, %d improved, %d paywalled, %d failed",
                                 es.checked, es.fetched, es.improved, es.paywalled, es.failed)
                    self._maybe_purge(cfg)
                except Exception:  # keep the loop alive; systemd restarts us only on a crash
                    log.exception("worker tick failed")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=cfg.settings.worker.tick_s)
                except TimeoutError:
                    pass
        finally:
            await fetcher.aclose()
            log.info("worker stopped")


def backup_db(data_dir: Path, dest_dir: Path, keep: int) -> Path:
    conn = db.connect(db.db_path(data_dir))
    try:
        dest = dest_dir / f"readwell-{now_iso()[:10]}.db"
        db.backup(conn, dest)
    finally:
        conn.close()
    olds = sorted(dest_dir.glob("readwell-????-??-??.db"))
    for old in olds[:-keep] if keep > 0 else []:
        old.unlink(missing_ok=True)
    return dest
