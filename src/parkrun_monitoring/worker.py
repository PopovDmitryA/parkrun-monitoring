"""Queue worker: claim a location, fetch its history, release, repeat.

Several workers run in parallel (one per VPN country on the server, plus
the Mac daemon when the site queue is idle). Coordination is the claims
table: a worker only ever fetches events it has claimed, so parallel
workers never duplicate work.

Claims are taken either directly in the local database (server workers,
which all share the canonical SQLite) or through ``PM_CLAIM_COMMAND`` — a
shell hook that forwards ``claim``/``release`` to the canonical database
over SSH (the Mac worker). If the hook fails, the worker stops: no
coordination means no guarantee against duplicates.
"""

from __future__ import annotations

import random
import sqlite3
import subprocess
import time
from datetime import datetime, timezone

import httpx

from . import claims
from .config import Config
from .fetch import make_client
from .history import sync_event_history

# Consecutive failures usually mean the WAF noticed this exit IP.
MAX_CONSECUTIVE_FAILURES = 3


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ClaimChannel:
    """Claim/release against the local DB or through PM_CLAIM_COMMAND."""

    def __init__(self, conn: sqlite3.Connection, config: Config, worker: str):
        self.conn = conn
        self.command = config.claim_command
        self.worker = worker
        self.ttl = config.claim_ttl_minutes
        self.first_pass_only = config.first_pass_only

    def claim(self) -> str | None:
        if not self.command:
            return claims.claim_next_event(
                self.conn, self.worker, self.ttl,
                first_pass_only=self.first_pass_only,
            )
        # Remote (Mac) claims land in the server's claim-one CLI, which reads
        # PM_FIRST_PASS_ONLY from the server's own .env — no flag to forward.
        result = subprocess.run(
            f"{self.command} claim {self.worker} {self.ttl}",
            shell=True, capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"claim command failed: {result.stderr.strip() or result.returncode}"
            )
        eventname = result.stdout.strip()
        return eventname or None

    def release(self, eventname: str) -> None:
        if not self.command:
            claims.release_event(self.conn, self.worker, eventname)
            return
        subprocess.run(
            f"{self.command} release {self.worker} {eventname}",
            shell=True, capture_output=True, text=True, timeout=60,
        )


def _pick_event(conn: sqlite3.Connection, eventname: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT e.id, e.eventname, e.long_name, c.url AS country_url
        FROM events e JOIN countries c ON c.code = e.country_code
        WHERE e.eventname = ? AND c.url IS NOT NULL
        """,
        (eventname,),
    ).fetchone()


def run_worker(
    conn: sqlite3.Connection,
    config: Config,
    *,
    worker: str,
    limit: int,
    delay: float,
    proxy: str | None = None,
    push_each: bool = False,
) -> dict:
    from .push import run_push

    run_id = conn.execute(
        "INSERT INTO worker_runs (worker, started_at) VALUES (?, ?)",
        (worker, _now()),
    ).lastrowid
    conn.commit()

    summary = {"synced": 0, "rows": 0, "failed": 0, "pushed": 0}
    status, error = "ok", None
    channel = ClaimChannel(conn, config, worker)
    consecutive_failures = 0

    def _pause() -> None:
        time.sleep(delay * random.uniform(0.85, 1.15))

    try:
        with make_client(config.user_agent, proxy=proxy) as client:
            for i in range(limit):
                if i:
                    _pause()
                eventname = channel.claim()
                if eventname is None:
                    print(f"[{worker}] queue drained", flush=True)
                    break
                event = _pick_event(conn, eventname)
                if event is None:
                    # Known to the canonical DB but not to this replica yet.
                    channel.release(eventname)
                    print(f"[{worker}] skip {eventname}: not in local db", flush=True)
                    continue
                try:
                    count = sync_event_history(conn, client, event)
                except httpx.HTTPError as exc:
                    conn.rollback()
                    channel.release(eventname)
                    summary["failed"] += 1
                    consecutive_failures += 1
                    print(f"[{worker}] fail {eventname} — {exc!r}", flush=True)
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        status = "aborted"
                        error = f"{MAX_CONSECUTIVE_FAILURES} consecutive failures (WAF?)"
                        print(f"[{worker}] aborting: {error}", flush=True)
                        break
                    continue
                consecutive_failures = 0
                summary["synced"] += 1
                summary["rows"] += count
                url = f"https://{event['country_url']}/{event['eventname']}/"
                name = event["long_name"] or event["eventname"]
                print(f"[{worker}] ok: {url} {name} — {count} runs", flush=True)
                if push_each and run_push(conn, config, quiet=True):
                    summary["pushed"] += 1
                channel.release(eventname)
    except Exception as exc:  # noqa: BLE001 — recorded, then re-raised
        status, error = "error", repr(exc)
        raise
    finally:
        conn.execute(
            "UPDATE worker_runs SET finished_at=?, status=?, synced=?, rows=?, "
            "failed=?, error=? WHERE id=?",
            (_now(), status, summary["synced"], summary["rows"],
             summary["failed"], error, run_id),
        )
        conn.commit()
    return summary
