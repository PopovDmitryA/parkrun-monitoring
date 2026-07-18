from pathlib import Path

import pytest

from parkrun_monitoring import archive, db


def snapshot(event_id: int, eventname: str, country: int = 79) -> dict:
    return {
        "countries": {str(country): {"url": "www.parkrun.ru", "bounds": [29.9, 45.0, 129.7, 62.0]}},
        "events": {
            "features": [
                {
                    "id": event_id,
                    "geometry": {"coordinates": [37.53, 55.88]},
                    "properties": {
                        "eventname": eventname,
                        "EventLongName": f"parkrun {eventname}",
                        "EventShortName": eventname,
                        "LocalisedEventLongName": None,
                        "countrycode": country,
                        "seriesid": 1,
                        "EventLocation": "Somewhere",
                    },
                }
            ]
        },
    }


@pytest.fixture
def conn(tmp_path: Path):
    conn = db.connect(tmp_path / "a.db")
    now = "2026-07-18T00:00:00Z"
    conn.execute(
        "INSERT INTO countries (code, url, is_active, first_seen, last_seen) "
        "VALUES (79, 'www.parkrun.ru', 0, ?, ?)", (now, now))
    return conn


def run_import(conn, monkeypatch, snapshots, **kwargs):
    monkeypatch.setattr(archive, "list_snapshots", lambda *a, **k: list(snapshots))
    monkeypatch.setattr(archive, "fetch_snapshot", lambda client, ts: snapshots[ts])
    monkeypatch.setattr(archive.time, "sleep", lambda _: None)
    return archive.import_archived_events(conn, client=None, delay=0, **kwargs)


def test_import_adds_closed_events_with_coordinates(conn, monkeypatch):
    snapshots = {"20191003000000": snapshot(9001, "bitsa")}
    summary = run_import(conn, monkeypatch, snapshots)
    assert summary["added"] == 1
    row = conn.execute("SELECT * FROM events WHERE id=9001").fetchone()
    assert row["eventname"] == "bitsa"
    assert (row["latitude"], row["longitude"]) == (55.88, 37.53)
    assert row["is_active"] == 0
    assert row["catalogue_source"] == "wayback"
    assert row["disappeared_at"] is not None
    country = conn.execute("SELECT * FROM countries WHERE code=79").fetchone()
    assert country["bounds_west"] == 29.9


def test_repeated_snapshots_extend_last_seen_without_duplicates(conn, monkeypatch):
    snapshots = {
        "20191003000000": snapshot(9001, "bitsa"),
        "20220210000000": snapshot(9001, "bitsa"),
    }
    summary = run_import(conn, monkeypatch, snapshots)
    assert summary["added"] == 1
    row = conn.execute("SELECT first_seen, last_seen FROM events WHERE id=9001").fetchone()
    assert row["first_seen"].startswith("2019-10-03")
    assert row["last_seen"].startswith("2022-02-10")


def test_country_filter_skips_other_countries(conn, monkeypatch):
    snapshots = {"20191003000000": snapshot(9002, "bushy", country=97)}
    summary = run_import(conn, monkeypatch, snapshots, country_code=79)
    assert summary["added"] == 0


def test_live_events_are_not_overwritten(conn, monkeypatch):
    now = "2026-07-18T00:00:00Z"
    conn.execute(
        "INSERT INTO events (id, eventname, country_code, series_id, is_active, "
        "first_seen, last_seen, catalogue_source) "
        "VALUES (9001, 'bitsa', 79, 1, 1, ?, ?, 'live')", (now, now))
    snapshots = {"20191003000000": snapshot(9001, "renamed")}
    summary = run_import(conn, monkeypatch, snapshots)
    assert summary["added"] == 0
    row = conn.execute("SELECT eventname, is_active, last_seen FROM events WHERE id=9001").fetchone()
    assert row["eventname"] == "bitsa" and row["is_active"] == 1
    assert row["last_seen"] == now  # archive must not rewind a live row
