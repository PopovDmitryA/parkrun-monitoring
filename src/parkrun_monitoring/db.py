"""SQLite schema and connection helpers.

The database is intentionally small: the events catalogue is stored as one
row per event (with activity flags), every catalogue change is appended to
``event_changes``, and weekly statistics live in a compact WITHOUT ROWID
table keyed by (country_code, week_date).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS countries (
    code        INTEGER PRIMARY KEY,
    url         TEXT,
    bounds_west REAL,
    bounds_south REAL,
    bounds_east REAL,
    bounds_north REAL,
    is_active   INTEGER NOT NULL DEFAULT 1,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id                  INTEGER PRIMARY KEY,  -- parkrun's own id from events.json
    eventname           TEXT NOT NULL,        -- URL slug
    long_name           TEXT,
    short_name          TEXT,
    localised_long_name TEXT,
    country_code        INTEGER NOT NULL,
    series_id           INTEGER NOT NULL,
    location            TEXT,
    latitude            REAL,
    longitude           REAL,
    is_active           INTEGER NOT NULL DEFAULT 1,
    first_seen          TEXT NOT NULL,
    last_seen           TEXT NOT NULL,
    disappeared_at      TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_eventname ON events (eventname);
CREATE INDEX IF NOT EXISTS ix_events_country ON events (country_code);

CREATE TABLE IF NOT EXISTS event_changes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    INTEGER NOT NULL,
    eventname   TEXT NOT NULL,
    change_type TEXT NOT NULL,  -- added | removed | reappeared | modified
    details     TEXT,           -- JSON {field: [old, new]} for "modified"
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS country_weekly_stats (
    country_code INTEGER NOT NULL,  -- 0 = worldwide totals
    week_date    TEXT NOT NULL,     -- ISO date of the Saturday
    events       INTEGER,
    finishers    INTEGER,
    volunteers   INTEGER,
    PRIMARY KEY (country_code, week_date)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS event_history (
    event_id          INTEGER NOT NULL,
    run_number        INTEGER NOT NULL,
    run_date          TEXT NOT NULL,
    finishers         INTEGER,
    volunteers        INTEGER,
    male_name         TEXT,
    male_athlete_id   INTEGER,
    male_time_sec     INTEGER,
    female_name       TEXT,
    female_athlete_id INTEGER,
    female_time_sec   INTEGER,
    PRIMARY KEY (event_id, run_number)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sync_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT NOT NULL,
    finished_at    TEXT,
    status         TEXT NOT NULL DEFAULT 'running',  -- running | ok | error
    events_total   INTEGER,
    added          INTEGER,
    removed        INTEGER,
    reappeared     INTEGER,
    modified       INTEGER,
    stats_new_rows INTEGER,
    error          TEXT
);
"""


# Columns added after the initial release; applied idempotently on connect.
_COLUMN_MIGRATIONS = (
    ("events", "history_synced_at", "TEXT"),
    ("events", "history_runs", "INTEGER"),
    ("countries", "stats_synced_at", "TEXT"),
    # "live" for events seen in the current catalogue, "wayback" for events
    # recovered from archived snapshots (closed countries and venues).
    ("events", "catalogue_source", "TEXT"),
)


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, column, ddl_type in _COLUMN_MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _apply_column_migrations(conn)
    conn.commit()
    return conn


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
