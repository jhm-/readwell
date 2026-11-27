"""``readwell`` command line.

    readwell worker              run the fetch loop (systemd: readwell-worker)
    readwell serve               run the web app      (systemd: readwell-web)
    readwell check               validate config and resolve every source's feed (no items stored)
    readwell discover URL        show the feeds Readwell finds for a page
    readwell run-once [ID ...]   fetch sources now (all, or the given ids) and store items
    readwell status              one line per source
    readwell screening [--test]  LLM spend, screening outcomes, topic match counts (--test: one LLM call)
    readwell rescreen [--days N] screen recent items again after editing topics.yaml
    readwell send ITEM_ID        make the Supernote PDF for one item now
    readwell migrate             create / upgrade the database
    readwell backup [DIR]        copy the database to DIR (default $READWELL_BACKUP_DIR)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__, db
from .config import ConfigError, default_config_dir, default_data_dir, load_config
from .discovery import discover, parse_feed
from .fetch.http import Fetcher
from .models import SourceError


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s" if os.environ.get("INVOCATION_ID")  # under systemd
        else "%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def cmd_worker(args: argparse.Namespace) -> int:
    from .worker import Worker

    asyncio.run(Worker(args.config_dir, args.data_dir).run_forever())
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .config import Settings
    from .web.app import create_app

    try:
        settings = load_config(args.config_dir, args.data_dir).settings
    except ConfigError as e:  # serve anyway; pages explain the problem (503)
        logging.getLogger(__name__).error("%s — starting with default settings", e)
        settings = Settings()
    host = args.host or settings.server.host
    port = args.port or settings.server.port
    uvicorn.run(create_app(args.config_dir, args.data_dir), host=host, port=port, log_level="info",
                proxy_headers=False, server_header=False)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config(args.config_dir, args.data_dir)
    print(f"config: {cfg.config_dir}{' (CACHED COPY)' if cfg.from_cache else ''}")
    print(f"sources: {len(cfg.sources)}  config errors: {len(cfg.errors)}")
    for e in cfg.errors:
        print(f"  ! {e}")
    todo = [s for s in cfg.sources.values() if (s.enabled or args.ids) and (not args.ids or s.id in args.ids)]
    # snappier than the worker: shorter per-request timeout, and a cap per source
    fetch_cfg = cfg.settings.fetch.model_copy(update={"timeout_s": min(cfg.settings.fetch.timeout_s, 15.0)})
    print(f"checking {len(todo)} sources (each result prints as it finishes; at most {args.timeout:.0f} s per source)\n",
          flush=True)

    rows: dict = {}
    try:
        conn = db.open_db(args.data_dir or default_data_dir())
        rows = {r["id"]: r for r in conn.execute("SELECT * FROM sources")}
    except Exception:  # noqa: BLE001 — no state yet is fine; non-feed checks then start from scratch
        pass
    blank = {"cursor": None, "baseline_done": 0, "resolved_url": None, "etag": None, "last_modified": None}

    async def run() -> int:
        bad = 0
        async with Fetcher(fetch_cfg) as f:
            async def other_kind(s):
                # email / numbered / listing: run the real fetch once, without storing anything
                from .sources import REGISTRY, SourceContext

                row = rows.get(s.id) or blank
                if s.kind == "numbered" and not row["cursor"]:
                    return s, "skip", "never run — its first run-once records the back catalogue"
                try:
                    res = await asyncio.wait_for(REGISTRY[s.kind](SourceContext(f, cfg, s, row)),
                                                 timeout=args.timeout)
                except TimeoutError:
                    return s, "FAIL", f"timed out after {args.timeout:.0f} s"
                except SourceError as e:
                    return s, "FAIL", str(e)
                n = len(res.items)
                what = {"numbered": "new document(s) waiting", "email": "message(s) since last run"}.get(s.kind, "items")
                return s, "ok", f"{n} {what}  [{s.kind}]"

            async def one(s):
                try:
                    return await check_one(s)
                except Exception as e:  # noqa: BLE001 — report it; never let one source end the check
                    return s, "FAIL", f"internal error: {type(e).__name__}: {e}"

            async def check_one(s):
                if not s.implemented:
                    return s, "skip", f"kind '{s.kind}' not built yet"
                if s.kind != "feed":
                    return await other_kind(s)
                if s.feed_url:
                    try:
                        r = await asyncio.wait_for(f.get(s.feed_url), timeout=args.timeout)
                    except TimeoutError:
                        return s, "FAIL", f"feed_url timed out after {args.timeout:.0f} s"
                    except SourceError as e:
                        return s, "FAIL", f"feed_url: {e}"
                    d = parse_feed(r.content, r.url, r.content_type)
                    if d is None:
                        return s, "FAIL", f"feed_url is not a feed ({r.content_type or 'unknown type'})"
                    return s, "ok", f"{s.feed_url}  [{d.get('version')}, {len(d.entries)} entries, set in config]"
                try:
                    found = await asyncio.wait_for(discover(f, s.url, limit=1), timeout=args.timeout)
                except TimeoutError:
                    return s, "FAIL", f"timed out after {args.timeout:.0f} s (site slow or blocking us)"
                except SourceError as e:
                    return s, "FAIL", str(e)
                if not found:
                    return s, "FAIL", "no feed found; add feed_url"
                d = found[0]
                return s, "ok", f"{d.url}  [{d.version}, {d.entries} entries, via {d.how}]"

            for fut in asyncio.as_completed([one(s) for s in todo]):
                s, status, msg = await fut
                bad += status == "FAIL"
                print(f"{status:4}  {s.id:28} {msg}", flush=True)
        print(f"\n{len(todo) - bad} ok / {bad} failed")
        return 1 if bad or cfg.errors else 0

    return asyncio.run(run())


def cmd_discover(args: argparse.Namespace) -> int:
    from .config import FetchCfg

    try:
        fetch_cfg = load_config(args.config_dir, args.data_dir).settings.fetch
    except ConfigError:
        fetch_cfg = FetchCfg()

    async def run() -> int:
        async with Fetcher(fetch_cfg) as f:
            try:
                found = await discover(f, args.url, limit=5)
            except SourceError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1
        if not found:
            print("no feeds found")
            return 1
        for d in found:
            print(f"{d.url}\n    {d.title or '(no title)'} — {d.version}, {d.entries} entries, via {d.how}")
        return 0

    return asyncio.run(run())


def cmd_run_once(args: argparse.Namespace) -> int:
    from .worker import Worker

    w = Worker(args.config_dir, args.data_dir)
    outcomes = asyncio.run(w.run_once(args.ids or None))
    bad = 0
    for o in sorted(outcomes, key=lambda o: o.source_id):
        if o.status == "error":
            bad += 1
            print(f"FAIL  {o.source_id:28} {o.error_kind}: {o.error}")
        elif o.status == "skipped":
            print(f"skip  {o.source_id:28} {o.error}")
        else:
            st = o.stats
            detail = "not modified" if st is None else f"{st.fetched} fetched, {st.new} new ({st.clustered} dup of other sources), {st.known} known"
            print(f"ok    {o.source_id:28} {detail}")
    print(f"\n{len(outcomes) - bad} ok / {bad} failed")
    return 1 if bad else 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config_dir, args.data_dir)
    conn = db.open_db(args.data_dir or default_data_dir())
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM sources")}
    for s in sorted(cfg.sources.values(), key=lambda s: s.id):
        r = rows.get(s.id)
        if r is None:
            print(f"{s.id:28} never run")
            continue
        n = conn.execute("SELECT COUNT(*) FROM items WHERE source_id = ?", (s.id,)).fetchone()[0]
        state = f"{r['error_kind']} x{r['consecutive_failures']}" if r["consecutive_failures"] else "ok"
        print(f"{s.id:28} {state:14} items={n:<5} last_ok={r['last_ok_at'] or '-':20} next={r['next_run_at'] or '-'}")
    return 0


def cmd_rescreen(args: argparse.Namespace) -> int:
    """Screen recent items again, e.g. after editing topics.yaml (the cache keeps unchanged ones free)."""
    from .screen.run import reset_for_rescreen
    from .worker import Worker

    w = Worker(args.config_dir, args.data_dir)
    cfg = w.reload_if_changed()
    n = reset_for_rescreen(w.conn, args.days, args.ids or None)
    print(f"{n} item(s) from the last {args.days} day(s) queued for screening")

    async def go() -> None:
        try:
            from .screen.run import screen_pending

            st = await screen_pending(w.conn, cfg, w.screener(cfg), max_batches=args.max_batches)
            print(f"screened {st.checked}: {st.llm_items} by LLM in {st.llm_calls} call(s) (${st.cost_usd:.4f}),"
                  f" {st.cached} from cache, {st.keywords} by keywords, {st.dropped} dropped, {st.muted} muted,"
                  f" {st.passthrough} passthrough, {st.deferred} deferred, {st.errors} error(s)")
        finally:
            await w.close()

    asyncio.run(go())
    return 0


def cmd_screening(args: argparse.Namespace) -> int:
    """Today's LLM spend, recent screening outcomes and topic match counts; --test makes one tiny call."""
    from .screen.llm import ScreenInput, make_screener
    from .screen.run import budget_day, cost, spent_today
    from .timeutil import iso_ago
    from .web import match

    cfg = load_config(args.config_dir, args.data_dir)
    conn = db.open_db(args.data_dir or default_data_dir())
    llm = cfg.settings.llm
    print(f"backend {llm.backend} · model {llm.model} · {llm.base_url or 'provider default URL'}")
    print(f"budget day {budget_day(cfg)}: spent ${spent_today(conn, cfg):.4f} of ${llm.daily_budget_usd:.2f}")
    print(f"topics: {len(cfg.topics.topics) if cfg.topics else 0} · views: {', '.join(cfg.views) or 'none'}")
    print("\nlast 24 h:")
    for r in conn.execute("SELECT COALESCE(screen_state, 'waiting') AS k, COUNT(*) AS n FROM items"
                          " WHERE is_lead = 1 AND state != 'baseline' AND first_seen_at >= ? GROUP BY k ORDER BY n DESC",
                          (iso_ago(days=1),)):
        print(f"  {r['k']:12} {r['n']}")
    rows = conn.execute("SELECT id, source_id FROM items WHERE is_lead = 1 AND screen_state = 'screened'"
                        " AND first_seen_at >= ?", (iso_ago(days=7),)).fetchall()
    sc = match.load_scores(conn, [r["id"] for r in rows])
    counts: dict[str, int] = {}
    for r in rows:
        for t in match.matches(cfg, r["source_id"], sc[r["id"]]):
            counts[t] = counts.get(t, 0) + 1
    if cfg.topics and cfg.topics.topics:
        print("\ntopic matches, last 7 days:")
        for t in cfg.topics.topics:
            print(f"  {t.id:28} {counts.get(t.id, 0)}")
    if not args.test:
        return 0
    screener = make_screener(llm)
    if screener is None:
        print("\nLLM backend is 'none'; nothing to test")
        return 1
    if not cfg.topics or not cfg.topics.topics:
        print("\nno topics to test against")
        return 1

    async def go() -> int:
        try:
            sample = ScreenInput("t", "Test", "Yukon government pauses new mineral staking near Dawson",
                                 None, "The Government of Yukon paused quartz claim staking over 3,400 km2 "
                                       "while the Dawson regional land use plan is completed.")
            res, usage = await screener.screen([sample], cfg.topics.topics, cfg.topics.jurisdictions)
        except Exception as e:  # noqa: BLE001 — this is a diagnostic; show whatever went wrong
            print(f"\nLLM test FAILED: {e}")
            return 1
        finally:
            await screener.aclose()
        print(f"\nLLM test ok: {usage.input_tokens} in / {usage.output_tokens} out tokens, ${cost(cfg, usage):.5f}")
        for r in res:
            print(f"  scores {r.scores}\n  reason {r.reason}")
        return 0

    return asyncio.run(go())


def cmd_send(args: argparse.Namespace) -> int:
    """Make the Supernote PDF for one item now (its id is in feed entry ids: …item/<id>)."""
    from .supernote import process_sends
    from .timeutil import now_iso

    cfg = load_config(args.config_dir, args.data_dir)
    conn = db.open_db(args.data_dir or default_data_dir())
    if conn.execute("SELECT 1 FROM items WHERE id = ?", (args.item_id,)).fetchone() is None:
        print(f"no item {args.item_id}", file=sys.stderr)
        return 1
    with db.tx(conn):
        conn.execute("INSERT INTO sends (item_id, requested_at) VALUES (?, ?)", (args.item_id, now_iso()))
    asyncio.run(process_sends(conn, cfg))
    r = conn.execute("SELECT path, error FROM sends WHERE item_id = ? ORDER BY id DESC LIMIT 1",
                     (args.item_id,)).fetchone()
    print(r["path"] or f"not sent: {r['error']}")
    return 0 if r["path"] else 1


def cmd_migrate(args: argparse.Namespace) -> int:
    conn = db.open_db(args.data_dir or default_data_dir())
    print(f"schema version {conn.execute('PRAGMA user_version').fetchone()[0]}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from .worker import backup_db

    dest = args.dest or os.environ.get("READWELL_BACKUP_DIR")
    if not dest:
        print("error: give a directory or set READWELL_BACKUP_DIR", file=sys.stderr)
        return 2
    keep = 14
    try:
        keep = load_config(args.config_dir, args.data_dir).settings.retention.backups_keep
    except ConfigError:
        pass
    path = backup_db(args.data_dir or default_data_dir(), Path(dest), keep)
    print(f"backed up to {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="readwell", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"readwell {__version__}")
    p.add_argument("--config-dir", type=Path, default=None, help="default: $READWELL_CONFIG_DIR or ./config")
    p.add_argument("--data-dir", type=Path, default=None, help="default: $READWELL_DATA_DIR or ./data")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("worker").set_defaults(fn=cmd_worker)
    sp = sub.add_parser("serve")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(fn=cmd_serve)
    sp = sub.add_parser("check")
    sp.add_argument("ids", nargs="*")
    sp.add_argument("--timeout", type=float, default=60.0, help="seconds allowed per source (default 60)")
    sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("discover")
    sp.add_argument("url")
    sp.set_defaults(fn=cmd_discover)
    sp = sub.add_parser("run-once")
    sp.add_argument("ids", nargs="*")
    sp.set_defaults(fn=cmd_run_once)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sp = sub.add_parser("rescreen", help="screen recent items again (after editing topics.yaml)")
    sp.add_argument("ids", nargs="*", help="only these sources")
    sp.add_argument("--days", type=int, default=3)
    sp.add_argument("--max-batches", type=int, default=100)
    sp.set_defaults(fn=cmd_rescreen)
    sp = sub.add_parser("screening", help="LLM spend, screening outcomes and topic match counts")
    sp.add_argument("--test", action="store_true", help="also make one small LLM call to check the setup")
    sp.set_defaults(fn=cmd_screening)
    sp = sub.add_parser("send", help="make the Supernote PDF for one item now")
    sp.add_argument("item_id", type=int)
    sp.set_defaults(fn=cmd_send)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    sp = sub.add_parser("backup")
    sp.add_argument("dest", nargs="?", type=Path)
    sp.set_defaults(fn=cmd_backup)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    if args.config_dir is None:
        args.config_dir = default_config_dir()
    if args.data_dir is None:
        args.data_dir = default_data_dir()
    try:
        return args.fn(args)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
