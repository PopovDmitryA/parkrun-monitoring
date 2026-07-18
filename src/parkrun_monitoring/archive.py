"""Recover closed events from archived copies of the events catalogue.

``events.json`` only lists events that are currently active, so venues that
shut down — and whole countries that left parkrun, such as Russia in 2022
and France — vanish from it without trace. The Internet Archive has been
snapshotting that file for years, and every snapshot carries the same
fields as the live one, coordinates included.

This module walks those snapshots oldest-first and inserts any event that
is missing from the local catalogue, marking it inactive and recording
``catalogue_source='wayback'``. Live catalogue rows are never overwritten:
the current feed remains the source of truth for anything still running.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone

import httpx

from .config import EVENTS_JSON_URL

CDX_URL = "http://web.archive.org/cdx/search/cdx"
SNAPSHOT_URL = "http://web.archive.org/web/{timestamp}id_/{url}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _snapshot_date(timestamp: str) -> str:
    return f"{timestamp[0:4]}-{timestamp[4:6]}-{timestamp[6:8]}T00:00:00Z"


def list_snapshots(
    client: httpx.Client, *, from_year: int | None = None, to_year: int | None = None
) -> list[str]:
    """Wayback timestamps of archived events.json copies, one per month."""
    params = {
        "url": "images.parkrun.com/events.json",
        "output": "json",
        "filter": "statuscode:200",
        "collapse": "timestamp:6",
    }
    if from_year:
        params["from"] = str(from_year)
    if to_year:
        params["to"] = str(to_year)
    rows = client.get(CDX_URL, params=params, timeout=90).raise_for_status().json()
    return [row[1] for row in rows[1:]]  # row[0] is the header


def fetch_snapshot(client: httpx.Client, timestamp: str) -> dict:
    url = SNAPSHOT_URL.format(timestamp=timestamp, url=EVENTS_JSON_URL)
    return client.get(url, timeout=120, follow_redirects=True).raise_for_status().json()


def import_archived_events(
    conn: sqlite3.Connection,
    client: httpx.Client,
    *,
    country_code: int | None = None,
    from_year: int | None = None,
    to_year: int | None = None,
    delay: float = 1.0,
) -> dict:
    """Insert events present in archived snapshots but missing locally."""
    snapshots = list_snapshots(client, from_year=from_year, to_year=to_year)
    known = {row[0] for row in conn.execute("SELECT id FROM events")}
    summary = {"snapshots": 0, "added": 0, "failed": 0}
    now = _now()

    for index, timestamp in enumerate(snapshots):
        if index:
            time.sleep(delay)
        try:
            data = fetch_snapshot(client, timestamp)
        except (httpx.HTTPError, ValueError) as exc:
            summary["failed"] += 1
            print(f"snapshot {timestamp}: {exc!r}", flush=True)
            continue
        summary["snapshots"] += 1
        seen_date = _snapshot_date(timestamp)
        added_here = 0

        for feature in data.get("events", {}).get("features", []):
            props = feature["properties"]
            code = int(props["countrycode"])
            if country_code is not None and code != country_code:
                continue
            event_id = int(feature["id"])
            if event_id in known:
                # Already known: refresh how long the archive saw it running.
                conn.execute(
                    "UPDATE events SET last_seen=? WHERE id=? AND catalogue_source='wayback'"
                    " AND last_seen < ?",
                    (seen_date, event_id, seen_date),
                )
                continue
            coords = (feature.get("geometry") or {}).get("coordinates") or (None, None)
            conn.execute(
                """
                INSERT INTO events (id, eventname, long_name, short_name,
                    localised_long_name, country_code, series_id, location,
                    latitude, longitude, is_active, first_seen, last_seen,
                    disappeared_at, catalogue_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'wayback')
                """,
                (event_id, props["eventname"], props.get("EventLongName"),
                 props.get("EventShortName"), props.get("LocalisedEventLongName"),
                 code, int(props["seriesid"]), props.get("EventLocation"),
                 coords[1], coords[0], seen_date, seen_date, now),
            )
            known.add(event_id)
            added_here += 1
            summary["added"] += 1

        # Countries that left parkrun keep no entry in the live feed; restore
        # their map bounds from the same snapshot.
        for code, info in data.get("countries", {}).items():
            bounds = info.get("bounds")
            if not bounds or len(bounds) != 4:
                continue
            if country_code is not None and int(code) != country_code:
                continue
            conn.execute(
                "UPDATE countries SET url=COALESCE(url, ?), bounds_west=?, "
                "bounds_south=?, bounds_east=?, bounds_north=? "
                "WHERE code=? AND bounds_west IS NULL",
                (info.get("url"), *bounds, int(code)),
            )

        conn.commit()
        print(f"snapshot {timestamp[:8]}: +{added_here} events", flush=True)

    return summary
