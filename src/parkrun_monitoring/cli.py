"""Command-line interface: ``parkrun-monitoring sync|status``."""

from __future__ import annotations

import argparse
import sys

from . import db, notify
from .config import load_config
from .history import run_history_sync
from .push import run_push
from .sync import check_gate, record_skipped_run, run_sync


def cmd_sync(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.db_path)
    skip_reason = check_gate(config)
    if skip_reason:
        record_skipped_run(conn, skip_reason)
        print(f"sync skipped: {skip_reason}")
        return 0
    try:
        changes = run_sync(
            conn,
            config,
            catalogue=not args.stats_only,
            stats=not args.catalogue_only,
        )
    except Exception as exc:
        message = f"parkrun-monitoring: синк упал с ошибкой — {exc!r}"
        if not args.no_notify:
            notify.send(config, message)
        print(message, file=sys.stderr)
        return 1

    print(
        f"sync ok: events={changes.events_total} "
        f"+{len(changes.added)} -{len(changes.removed)} "
        f"~{len(changes.modified)} stats_rows+={changes.stats_new_rows} "
        f"(countries={changes.stats_countries})"
    )
    text = notify.format_message(changes)
    if text and not args.no_notify:
        notify.send(config, text)
    return 0


def cmd_fetch_history(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.db_path)
    skip_reason = check_gate(config)
    if skip_reason:
        print(f"fetch-history skipped: {skip_reason}")
        return 0
    summary = run_history_sync(
        conn,
        config,
        limit=args.limit,
        delay=args.delay if args.delay is not None else config.history_delay,
        eventname=args.event,
    )
    print(
        f"fetch-history done: {summary['synced']} events, "
        f"{summary['rows']} history rows, {summary['failed']} failed"
    )
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.db_path)
    return 0 if run_push(conn, config) else 1


def cmd_status(_: argparse.Namespace) -> int:
    config = load_config()
    conn = db.connect(config.db_path)
    events = conn.execute(
        "SELECT COUNT(*), SUM(is_active) FROM events"
    ).fetchone()
    stats = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT country_code), MAX(week_date) "
        "FROM country_weekly_stats"
    ).fetchone()
    history = conn.execute(
        "SELECT COUNT(*), (SELECT COUNT(*) FROM events "
        "WHERE history_synced_at IS NOT NULL) FROM event_history"
    ).fetchone()
    last_run = conn.execute(
        "SELECT started_at, status FROM sync_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    print(f"db: {config.db_path}")
    print(f"events: {events[0]} total, {events[1] or 0} active")
    print(f"weekly stats: {stats[0]} rows, {stats[1]} countries, up to {stats[2]}")
    print(f"event history: {history[0]} rows across {history[1]} synced events")
    print(f"last run: {last_run['started_at']} [{last_run['status']}]" if last_run else "last run: never")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="parkrun-monitoring")
    sub = parser.add_subparsers(dest="command", required=True)

    sync_parser = sub.add_parser("sync", help="fetch catalogue + weekly stats, record changes")
    sync_parser.add_argument("--catalogue-only", action="store_true")
    sync_parser.add_argument("--stats-only", action="store_true")
    sync_parser.add_argument("--no-notify", action="store_true")
    sync_parser.set_defaults(func=cmd_sync)

    history_parser = sub.add_parser(
        "fetch-history", help="walk eventhistory summaries into the database"
    )
    history_parser.add_argument("--limit", type=int, default=25)
    history_parser.add_argument(
        "--delay", type=float, default=None,
        help="seconds between requests (default: PM_HISTORY_DELAY or 30)",
    )
    history_parser.add_argument("--event", help="sync a single event by slug")
    history_parser.set_defaults(func=cmd_fetch_history)

    push_parser = sub.add_parser(
        "push", help="push fresh stats/history to the canonical DB via PM_PUSH_COMMAND"
    )
    push_parser.set_defaults(func=cmd_push)

    status_parser = sub.add_parser("status", help="print database summary")
    status_parser.set_defaults(func=cmd_status)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
