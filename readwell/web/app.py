"""FastAPI app: feeds for RSS readers, OPML, a JSON health check and a small status page."""

from __future__ import annotations

import threading
import time
from html import escape
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response

from .. import __version__, db
from ..config import Config, ConfigError, default_config_dir, default_data_dir, fingerprint, load_config
from ..timeutil import iso_ago
from . import feeds

ATOM = "application/atom+xml; charset=utf-8"


class ConfigHolder:
    """Loads config lazily and re-checks for changes at most every ``check_s`` seconds."""

    def __init__(self, config_dir: Path, data_dir: Path, check_s: float = 30.0):
        self.config_dir, self.data_dir, self.check_s = config_dir, data_dir, check_s
        self._cfg: Config | None = None
        self._checked = 0.0
        self._lock = threading.Lock()

    def get(self) -> Config:
        with self._lock:
            stale = time.monotonic() - self._checked > self.check_s
            if self._cfg is None or (stale and (fingerprint(self.config_dir) != self._cfg.fingerprint
                                                or self._cfg.from_cache)):
                self._cfg = load_config(self.config_dir, self.data_dir)
            if stale:
                self._checked = time.monotonic()
            return self._cfg


def create_app(config_dir: Path | None = None, data_dir: Path | None = None) -> FastAPI:
    config_dir = config_dir or default_config_dir()
    data_dir = data_dir or default_data_dir()
    holder = ConfigHolder(config_dir, data_dir)
    db.open_db(data_dir).close()  # create / migrate the database once at startup

    app = FastAPI(title="Readwell", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)

    @app.exception_handler(ConfigError)
    def config_unavailable(request: Request, exc: ConfigError) -> PlainTextResponse:
        return PlainTextResponse(f"Readwell config unavailable: {exc}\n", status_code=503,
                                 headers={"Retry-After": "60"})

    def conn():
        return db.connect(db.db_path(data_dir), readonly=True)

    def atom(content: bytes) -> Response:
        return Response(content, media_type=ATOM, headers={"Cache-Control": "max-age=60"})

    @app.get("/feeds/all.atom")
    def all_feed(request: Request) -> Response:
        c = conn()
        try:
            return atom(feeds.items_feed(c, holder.get(), str(request.url)))
        finally:
            c.close()

    @app.get("/feeds/source/{source_id}.atom")
    def source_feed(source_id: str, request: Request) -> Response:
        cfg = holder.get()
        if source_id not in cfg.sources:
            raise HTTPException(404, f"unknown source {source_id!r}")
        c = conn()
        try:
            return atom(feeds.items_feed(c, cfg, str(request.url), source_id=source_id))
        finally:
            c.close()

    @app.get("/feeds/health.atom")
    def health_feed(request: Request) -> Response:
        c = conn()
        try:
            return atom(feeds.health_feed(c, holder.get(), str(request.url)))
        finally:
            c.close()

    @app.get("/opml")
    def opml(request: Request) -> Response:
        return Response(feeds.opml(holder.get(), str(request.base_url)), media_type="text/x-opml; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="readwell.opml"'})

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        cfg = holder.get()
        c = conn()
        try:
            n_items = c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            recent = c.execute("SELECT MAX(last_run_at) FROM sources").fetchone()[0]
            probs = feeds.problems(c, cfg)
        finally:
            c.close()
        return JSONResponse({
            "ok": True,
            "version": __version__,
            "config_dir": str(cfg.config_dir),
            "config_from_cache": cfg.from_cache,
            "config_errors": len(cfg.errors),
            "sources": len(cfg.sources),
            "items": n_items,
            "last_fetch": recent,
            "problems": len(probs),
        })

    @app.get("/", response_class=HTMLResponse)
    def status() -> HTMLResponse:
        cfg = holder.get()
        c = conn()
        try:
            rows = {r["id"]: r for r in c.execute("SELECT * FROM sources")}
            counts = {r["source_id"]: r["n"] for r in c.execute(
                "SELECT source_id, COUNT(*) AS n FROM items WHERE first_seen_at >= ? GROUP BY source_id",
                (iso_ago(days=7),))}
            probs = feeds.problems(c, cfg)
        finally:
            c.close()
        return HTMLResponse(_status_html(cfg, rows, counts, probs))

    return app


def _status_html(cfg: Config, rows: dict, counts: dict, probs: list) -> str:
    def cell(v: object) -> str:
        return f"<td>{escape('' if v is None else str(v))}</td>"

    trs = []
    for s in sorted(cfg.sources.values(), key=lambda s: s.id):
        r = rows.get(s.id)
        state = "disabled" if not s.enabled else ("not built yet" if not s.implemented else "")
        if r is not None and r["consecutive_failures"]:
            state = f"{r['error_kind']} ×{r['consecutive_failures']}"
        elif r is not None and r["last_ok_at"]:
            state = state or "ok"
        trs.append(
            "<tr>" + cell(s.id) + cell(s.kind) + cell(state)
            + cell((r["resolved_url"] if r is not None else None) or s.feed_url or "")
            + cell(r["last_ok_at"] if r is not None else "") + cell(counts.get(s.id, 0))
            + cell((r["last_error"] or "")[:160] if r is not None else "") + "</tr>"
        )
    prob_html = "".join(f"<li><b>{escape(p.title)}</b></li>" for p in probs) or "<li>None</li>"
    cache_note = " <b>(cached copy — config share unavailable)</b>" if cfg.from_cache else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Readwell</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font:14px/1.4 system-ui,sans-serif;margin:16px;color:#222;background:#fff}}
table{{border-collapse:collapse;width:100%}} td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left;vertical-align:top}}
th{{background:#f4f4f4}} code{{background:#f4f4f4;padding:1px 3px}}
@media (prefers-color-scheme: dark){{body{{color:#ddd;background:#161616}} th,code{{background:#262626}} td,th{{border-color:#333}}}}
</style></head><body>
<h1>Readwell <small>{escape(__version__)}</small></h1>
<p>Feeds: <a href="/feeds/all.atom">all.atom</a> · <a href="/feeds/health.atom">health.atom</a> ·
<a href="/opml">OPML</a> · <a href="/healthz">healthz</a></p>
<p>Config: <code>{escape(str(cfg.config_dir))}</code>{cache_note} — {len(cfg.sources)} sources</p>
<h2>Problems</h2><ul>{prob_html}</ul>
<h2>Sources</h2>
<table><tr><th>id</th><th>kind</th><th>state</th><th>feed</th><th>last ok</th><th>items 7d</th><th>last error</th></tr>
{''.join(trs)}</table></body></html>"""
