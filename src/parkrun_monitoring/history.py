"""Level 1: per-event run history (the eventhistory summary pages).

One request per event yields the full history of that event: run number,
date, finisher and volunteer counts, first finishers with times. Events are
processed oldest-synced-first, and every pass stamps
``events.history_synced_at`` so the database always records when each
event's summary was last walked.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import httpx

from .config import Config
from .fetch import fetch_event_history, make_client

# Consecutive failures usually mean the WAF is on to us — stop early.
MAX_CONSECUTIVE_FAILURES = 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def pick_events_for_history(
    conn: sqlite3.Connection, limit: int, eventname: str | None = None
) -> list[sqlite3.Row]:
    """Active events joined to a fetchable country URL, stalest history first."""
    query = """
        SELECT e.id, e.eventname, e.long_name, c.url AS country_url
        FROM events e JOIN countries c ON c.code = e.country_code
        WHERE e.is_active = 1 AND c.url IS NOT NULL
    """
    params: list[object] = []
    if eventname:
        query += " AND e.eventname = ?"
        params.append(eventname)
    query += """
        ORDER BY e.history_synced_at IS NOT NULL, e.history_synced_at, e.id
        LIMIT ?
    """
    params.append(limit)
    return conn.execute(query, params).fetchall()


def sync_event_history(
    conn: sqlite3.Connection, client: httpx.Client, event: sqlite3.Row
) -> int:
    rows = fetch_event_history(client, event["country_url"], event["eventname"])
    for row in rows:
        conn.execute(
            """
            INSERT INTO event_history (event_id, run_number, run_date, finishers,
                volunteers, male_name, male_athlete_id, male_time_sec,
                female_name, female_athlete_id, female_time_sec)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id, run_number) DO UPDATE SET
                run_date=excluded.run_date, finishers=excluded.finishers,
                volunteers=excluded.volunteers, male_name=excluded.male_name,
                male_athlete_id=excluded.male_athlete_id,
                male_time_sec=excluded.male_time_sec,
                female_name=excluded.female_name,
                female_athlete_id=excluded.female_athlete_id,
                female_time_sec=excluded.female_time_sec
            """,
            (event["id"], row.run_number, row.run_date, row.finishers,
             row.volunteers, row.male_name, row.male_athlete_id,
             row.male_time_sec, row.female_name, row.female_athlete_id,
             row.female_time_sec),
        )
    conn.execute(
        "UPDATE events SET history_synced_at=?, history_runs=? WHERE id=?",
        (_now(), len(rows), event["id"]),
    )
    conn.commit()
    return len(rows)


def run_history_sync(
    conn: sqlite3.Connection,
    config: Config,
    *,
    limit: int,
    delay: float,
    eventname: str | None = None,
    push_each: bool = False,
) -> dict:
    """Walk event summaries; with push_each every event lands on the canonical
    database as soon as it is fetched, so an interrupted run keeps its work."""
    from .push import run_push

    events = pick_events_for_history(conn, limit, eventname)
    summary = {"synced": 0, "rows": 0, "failed": 0, "pushed": 0}
    consecutive_failures = 0
    with make_client(config.user_agent) as client:
        for i, event in enumerate(events):
            if i:
                time.sleep(delay)
            try:
                count = sync_event_history(conn, client, event)
            except httpx.HTTPError as exc:
                conn.rollback()
                summary["failed"] += 1
                consecutive_failures += 1
                print(f"history fail: {event['eventname']} — {exc!r}", flush=True)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print("history: aborting, WAF likely", flush=True)
                    break
                continue
            consecutive_failures = 0
            summary["synced"] += 1
            summary["rows"] += count
            url = f"https://{event['country_url']}/{event['eventname']}/"
            name = event["long_name"] or event["eventname"]
            print(f"history ok: {url} {name} — {count} runs", flush=True)
            if push_each and run_push(conn, config, quiet=True):
                summary["pushed"] += 1
    return summary
