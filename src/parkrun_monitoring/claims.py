"""Atomic event claims so parallel workers never fetch the same location.

The claim state lives in two columns on ``events`` (``claimed_by``,
``claimed_at``). A claim is taken inside ``BEGIN IMMEDIATE`` — SQLite's
write lock makes the pick-and-mark atomic across processes, which is all
the coordination N local workers need. Remote workers (the Mac daemon)
claim through the same functions via the ``claim-one``/``release-claim``
CLI over SSH, so every consumer of the queue shares one source of truth.

A claim expires after a TTL: a worker that died mid-fetch releases its
event automatically, and because history writes are idempotent upserts, a
rare double-fetch after expiry is harmless.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def claim_next_event(
    conn: sqlite3.Connection, worker: str, ttl_minutes: int
) -> str | None:
    """Claim the stalest free event and return its eventname (None = drained)."""
    expiry = _iso(_now() - timedelta(minutes=ttl_minutes))
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            """
            SELECT e.eventname FROM events e
            JOIN countries c ON c.code = e.country_code
            WHERE e.is_active = 1 AND c.url IS NOT NULL
              AND (e.claimed_at IS NULL OR e.claimed_at < ?)
            ORDER BY e.history_synced_at IS NOT NULL, e.history_synced_at, e.id
            LIMIT 1
            """,
            (expiry,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        conn.execute(
            "UPDATE events SET claimed_by=?, claimed_at=? WHERE eventname=?",
            (worker, _iso(_now()), row["eventname"]),
        )
        conn.commit()
        return row["eventname"]
    except Exception:
        conn.rollback()
        raise


def release_event(conn: sqlite3.Connection, worker: str, eventname: str) -> None:
    """Free the claim; only the owner's claim is cleared."""
    conn.execute(
        "UPDATE events SET claimed_by=NULL, claimed_at=NULL "
        "WHERE eventname=? AND claimed_by=?",
        (eventname, worker),
    )
    conn.commit()


def active_claims(conn: sqlite3.Connection, ttl_minutes: int) -> list[sqlite3.Row]:
    expiry = _iso(_now() - timedelta(minutes=ttl_minutes))
    return conn.execute(
        "SELECT eventname, claimed_by, claimed_at FROM events "
        "WHERE claimed_at IS NOT NULL AND claimed_at >= ? ORDER BY claimed_at",
        (expiry,),
    ).fetchall()
