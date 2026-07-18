"""Push locally collected data to the canonical (server) database.

The tool can run in two roles: a server instance that owns the canonical
database, and a collector instance (e.g. a laptop whose IP is in better
standing with the WAF) that gathers weekly stats and event history locally.
``push`` exports the collector's fresh rows as portable SQL and pipes it to
``PM_PUSH_COMMAND`` — any command that applies stdin SQL to the canonical
database (typically a small ssh wrapper).

Only data the collector owns is pushed: weekly stats, country stats stamps,
and event history with its sync stamps. The events catalogue itself stays
owned by the canonical side.
"""

from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone

from .config import Config
from .db import kv_get, kv_set

LAST_PUSH_KEY = "last_push_at"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sql_value(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _insert_or_replace(table: str, columns: list[str], row: sqlite3.Row) -> str:
    values = ", ".join(_sql_value(row[c]) for c in columns)
    return f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({values});"


def build_push_sql(conn: sqlite3.Connection) -> tuple[str, int]:
    """Return (sql, pushed_events_count) for everything newer than the watermark."""
    watermark = kv_get(conn, LAST_PUSH_KEY) or ""
    lines = ["BEGIN;"]

    stats_columns = ["country_code", "week_date", "events", "finishers", "volunteers"]
    for row in conn.execute("SELECT * FROM country_weekly_stats"):
        lines.append(_insert_or_replace("country_weekly_stats", stats_columns, row))
    for row in conn.execute(
        "SELECT code, stats_synced_at FROM countries WHERE stats_synced_at IS NOT NULL"
    ):
        lines.append(
            f"UPDATE countries SET stats_synced_at={_sql_value(row['stats_synced_at'])} "
            f"WHERE code={row['code']};"
        )

    fresh_events = conn.execute(
        "SELECT id, history_synced_at, history_runs FROM events "
        "WHERE history_synced_at IS NOT NULL AND history_synced_at > ?",
        (watermark,),
    ).fetchall()
    history_columns = [
        "event_id", "run_number", "run_date", "finishers", "volunteers",
        "male_name", "male_athlete_id", "male_time_sec",
        "female_name", "female_athlete_id", "female_time_sec",
    ]
    for event in fresh_events:
        lines.append(
            f"UPDATE events SET history_synced_at={_sql_value(event['history_synced_at'])}, "
            f"history_runs={_sql_value(event['history_runs'])} WHERE id={event['id']};"
        )
        for row in conn.execute(
            "SELECT * FROM event_history WHERE event_id=?", (event["id"],)
        ):
            lines.append(_insert_or_replace("event_history", history_columns, row))

    lines.append("COMMIT;")
    return "\n".join(lines), len(fresh_events)


def run_push(conn: sqlite3.Connection, config: Config) -> bool:
    if not config.push_command:
        print("push: PM_PUSH_COMMAND is not configured — nothing to do")
        return False
    started = _now()
    sql, fresh_events = build_push_sql(conn)
    result = subprocess.run(
        config.push_command,
        shell=True,
        input=sql,
        text=True,
        capture_output=True,
        timeout=600,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        print(f"push FAILED (exit {result.returncode}): {detail}")
        return False
    kv_set(conn, LAST_PUSH_KEY, started)
    conn.commit()
    size_kb = len(sql) // 1024
    print(f"push ok: {fresh_events} events with fresh history, {size_kb} KB SQL")
    return True
