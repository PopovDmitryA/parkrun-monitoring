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


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
