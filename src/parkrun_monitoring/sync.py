"""Synchronisation of the events catalogue and weekly statistics into SQLite."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .config import Config, GLOBAL_COUNTRY_CODE, KNOWN_CLOSED_COUNTRIES
from .fetch import (
    CatalogueEvent,
    fetch_catalogue,
    fetch_country_stats,
    make_client,
)

# Catalogue fields whose changes are recorded as "modified" events.
TRACKED_FIELDS = (
    "eventname",
    "long_name",
    "short_name",
    "localised_long_name",
    "country_code",
    "series_id",
    "location",
    "latitude",
    "longitude",
)
COORD_EPSILON = 1e-6


@dataclass
class ChangeSet:
    initial_import: bool = False
    added: list[CatalogueEvent] = field(default_factory=list)
    removed: list[sqlite3.Row] = field(default_factory=list)
    reappeared: list[CatalogueEvent] = field(default_factory=list)
    modified: list[tuple[CatalogueEvent, dict]] = field(default_factory=list)
    events_total: int = 0
    stats_new_rows: int = 0
    stats_countries: int = 0
    stats_failed_countries: list[int] = field(default_factory=list)

    @property
    def has_catalogue_changes(self) -> bool:
        return bool(self.added or self.removed or self.reappeared or self.modified)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _field_diff(row: sqlite3.Row, event: CatalogueEvent) -> dict:
    diff = {}
    for name in TRACKED_FIELDS:
        old, new = row[name], getattr(event, name)
        if name in ("latitude", "longitude") and old is not None and new is not None:
            if abs(old - new) < COORD_EPSILON:
                continue
        if old != new:
            diff[name] = [old, new]
    return diff


def sync_catalogue(conn: sqlite3.Connection, client: httpx.Client) -> ChangeSet:
    countries, events = fetch_catalogue(client)
    now = _now()
    changes = ChangeSet(events_total=len(events))
    changes.initial_import = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

    for country in countries:
        bounds = country.bounds or (None, None, None, None)
        conn.execute(
            """
            INSERT INTO countries (code, url, bounds_west, bounds_south, bounds_east,
                                   bounds_north, is_active, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                url=excluded.url, bounds_west=excluded.bounds_west,
                bounds_south=excluded.bounds_south, bounds_east=excluded.bounds_east,
                bounds_north=excluded.bounds_north, is_active=1, last_seen=excluded.last_seen
            """,
            (country.code, country.url, *bounds, now, now),
        )
    for code, url in KNOWN_CLOSED_COUNTRIES.items():
        conn.execute(
            """
            INSERT INTO countries (code, url, is_active, first_seen, last_seen)
            VALUES (?, ?, 0, ?, ?) ON CONFLICT(code) DO NOTHING
            """,
            (code, url, now, now),
        )

    existing = {row["id"]: row for row in conn.execute("SELECT * FROM events")}
    seen_ids = set()

    for event in events:
        seen_ids.add(event.id)
        row = existing.get(event.id)
        if row is None:
            conn.execute(
                """
                INSERT INTO events (id, eventname, long_name, short_name,
                                    localised_long_name, country_code, series_id,
                                    location, latitude, longitude, is_active,
                                    first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (event.id, event.eventname, event.long_name, event.short_name,
                 event.localised_long_name, event.country_code, event.series_id,
                 event.location, event.latitude, event.longitude, now, now),
            )
            changes.added.append(event)
            if not changes.initial_import:
                _log_change(conn, event.id, event.eventname, "added", None, now)
            continue

        diff = _field_diff(row, event)
        if diff:
            changes.modified.append((event, diff))
            _log_change(conn, event.id, event.eventname, "modified", diff, now)
        if not row["is_active"]:
            changes.reappeared.append(event)
            _log_change(conn, event.id, event.eventname, "reappeared", None, now)
        conn.execute(
            """
            UPDATE events SET eventname=?, long_name=?, short_name=?,
                localised_long_name=?, country_code=?, series_id=?, location=?,
                latitude=?, longitude=?, is_active=1, last_seen=?, disappeared_at=NULL
            WHERE id=?
            """,
            (event.eventname, event.long_name, event.short_name,
             event.localised_long_name, event.country_code, event.series_id,
             event.location, event.latitude, event.longitude, now, event.id),
        )

    for event_id, row in existing.items():
        if event_id in seen_ids or not row["is_active"]:
            continue
        conn.execute(
            "UPDATE events SET is_active=0, disappeared_at=? WHERE id=?",
            (now, event_id),
        )
        changes.removed.append(row)
        _log_change(conn, event_id, row["eventname"], "removed", None, now)

    if changes.initial_import:
        changes.added = []  # a first import is a baseline, not a change
    return changes


def _log_change(
    conn: sqlite3.Connection,
    event_id: int,
    eventname: str,
    change_type: str,
    details: dict | None,
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO event_changes (event_id, eventname, change_type, details, detected_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_id, eventname, change_type,
         json.dumps(details, ensure_ascii=False) if details else None, now),
    )


def sync_weekly_stats(
    conn: sqlite3.Connection, client: httpx.Client, changes: ChangeSet, delay: float
) -> None:
    codes = [GLOBAL_COUNTRY_CODE] + [
        row["code"]
        for row in conn.execute("SELECT code, url FROM countries ORDER BY code")
        if row["url"] is not None or row["code"] in KNOWN_CLOSED_COUNTRIES
    ]
    for i, code in enumerate(codes):
        if i:
            time.sleep(delay)
        try:
            stats = fetch_country_stats(client, code)
        except httpx.HTTPError:
            changes.stats_failed_countries.append(code)
            continue
        changes.stats_countries += 1
        for row in stats:
            cursor = conn.execute(
                """
                INSERT INTO country_weekly_stats (country_code, week_date, events,
                                                  finishers, volunteers)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(country_code, week_date) DO UPDATE SET
                    events=excluded.events, finishers=excluded.finishers,
                    volunteers=excluded.volunteers
                WHERE events IS NOT excluded.events
                   OR finishers IS NOT excluded.finishers
                   OR volunteers IS NOT excluded.volunteers
                """,
                (code, row.week_date, row.events, row.finishers, row.volunteers),
            )
            changes.stats_new_rows += cursor.rowcount


def check_gate(config: Config) -> str | None:
    """Run the optional external gate command before syncing.

    Returns None when the sync may proceed, or a human-readable reason to
    skip. This lets a deployment coordinate with other parkrun tooling on
    the same host: e.g. exit non-zero from the gate while another scraper
    is serving a ban cooldown, and this sync will stand down entirely.
    """
    if not config.gate_command:
        return None
    result = subprocess.run(
        config.gate_command,
        shell=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode == 0:
        return None
    detail = (result.stdout + result.stderr).strip()
    return f"gate command exited {result.returncode}" + (f": {detail}" if detail else "")


def record_skipped_run(conn: sqlite3.Connection, reason: str) -> None:
    conn.execute(
        "INSERT INTO sync_runs (started_at, finished_at, status, error) "
        "VALUES (?, ?, 'skipped', ?)",
        (_now(), _now(), reason),
    )
    conn.commit()


def run_sync(
    conn: sqlite3.Connection,
    config: Config,
    *,
    catalogue: bool = True,
    stats: bool = True,
) -> ChangeSet:
    started = _now()
    run_id = conn.execute(
        "INSERT INTO sync_runs (started_at) VALUES (?)", (started,)
    ).lastrowid
    conn.commit()

    changes = ChangeSet()
    try:
        with make_client(config.user_agent) as client:
            if catalogue:
                changes = sync_catalogue(conn, client)
            if stats:
                sync_weekly_stats(conn, client, changes, config.request_delay)
    except Exception as exc:
        conn.execute(
            "UPDATE sync_runs SET finished_at=?, status='error', error=? WHERE id=?",
            (_now(), repr(exc), run_id),
        )
        conn.commit()
        raise

    conn.execute(
        """
        UPDATE sync_runs SET finished_at=?, status='ok', events_total=?, added=?,
            removed=?, reappeared=?, modified=?, stats_new_rows=?
        WHERE id=?
        """,
        (_now(), changes.events_total, len(changes.added), len(changes.removed),
         len(changes.reappeared), len(changes.modified), changes.stats_new_rows,
         run_id),
    )
    conn.commit()
    return changes
