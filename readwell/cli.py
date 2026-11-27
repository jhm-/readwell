"""``readwell`` command line.

    readwell worker              run the fetch loop (systemd: readwell-worker)
    readwell serve               run the web app      (systemd: readwell-web)
    readwell check               validate config and resolve every source's feed (no items stored)
    readwell discover URL        show the feeds Readwell finds for a page
    readwell run-once [ID ...]   fetch sources now (all, or the given ids) and store items
    readwell status              one line per source
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
from .discovery import discover
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
    todo = [s for s in cfg.sources.values() if not args.ids or s.id in args.ids]

    async def run() -> int:
        bad = 0
        async with Fetcher(cfg.settings.fetch) as f:
            async def one(s):
                if not s.implemented:
                    return s, "skip", f"kind '{s.kind}' not built yet"
                if s.feed_url:
                    return s, "ok", f"{s.feed_url} (set in config)"
                try:
                    found = await discover(f, s.url, limit=1)
                except SourceError as e:
                    return s, "FAIL", str(e)
                if not found:
                    return s, "FAIL", "no feed found; add feed_url"
                d = found[0]
                return s, "ok", f"{d.url}  [{d.version}, {d.entries} entries, via {d.how}]"

            for s, status, msg in await asyncio.gather(*(one(s) for s in todo)):
                bad += status == "FAIL"
                print(f"{status:4}  {s.id:28} {msg}")
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
    sp.set_defaults(fn=cmd_check)
    sp = sub.add_parser("discover")
    sp.add_argument("url")
    sp.set_defaults(fn=cmd_discover)
    sp = sub.add_parser("run-once")
    sp.add_argument("ids", nargs="*")
    sp.set_defaults(fn=cmd_run_once)
    sub.add_parser("status").set_defaults(fn=cmd_status)
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
